"""
Management command: run_escalation_check

Scans unresolved CallActionToken rows and advances the per-token
escalation level when due:

    Level 0 → initial alert sent (when token created)
    Level 1 → 15 min:  reminder push to owner (different vibrate pattern)
    Level 2 → 30 min:  reminder push + auto-SMS to caller
    Level 3 → 60 min:  final email to owner (dashboard pulse is CSS-driven)

State-based, not window-based — each level fires exactly once per token even
if the cron misfires or runs late. Safe to run more often than every 5 min.

Schedule via Render cron every 5 minutes:
    schedule: "*/5 * * * *"
"""
import logging
import threading

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from restaurants.models import CallActionToken
from restaurants.push import send_push

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Advance escalation level on pending CallActionToken rows."

    def handle(self, *args, **options):
        now = timezone.now()
        pending = (
            CallActionToken.objects
            .filter(used_at__isnull=True, expires_at__gt=now)
            .select_related("call_detail", "restaurant")
        )

        # Lazy-import view-level helpers to avoid circular imports at module load
        from restaurants.views import (
            _send_call_alert_email,
            _send_sms_via_twilio,
            _send_reservation_alert_email,
            _send_complaint_alert_email,
            _send_followup_alert_email,
        )

        advanced = {1: 0, 2: 0, 3: 0}

        for t in pending:
            age_min = t.age_minutes

            if age_min >= 15 and t.escalation_level < 1:
                _fire_reminder_push(t)
                t.escalation_level = 1
                t.save(update_fields=["escalation_level"])
                advanced[1] += 1

            if age_min >= 30 and t.escalation_level < 2:
                _fire_auto_sms(t, _send_sms_via_twilio)
                t.escalation_level = 2
                t.save(update_fields=["escalation_level"])
                advanced[2] += 1

            if age_min >= 60 and t.escalation_level < 3:
                _fire_final_alert(
                    t,
                    _send_reservation_alert_email,
                    _send_complaint_alert_email,
                    _send_followup_alert_email,
                )
                t.escalation_level = 3
                t.save(update_fields=["escalation_level"])
                advanced[3] += 1

        msg = (
            f"escalation_check: advanced "
            f"{advanced[1]} → L1 (15min), "
            f"{advanced[2]} → L2 (30min), "
            f"{advanced[3]} → L3 (60min)"
        )
        logger.info(msg)
        self.stdout.write(self.style.SUCCESS(msg))


# ─── Level handlers ──────────────────────────────────────────────────────────

def _caller_label(t):
    return (
        t.call_detail.caller_name
        or t.call_detail.caller_phone
        or "el cliente"
    )


def _fire_reminder_push(t: CallActionToken) -> None:
    """L1: gentle reminder with distinct vibrate (urgency=high bypasses throttle)."""
    title_by_type = {
        t.ACTION_RESERVATION: "⏰ Reserva esperando respuesta",
        t.ACTION_COMPLAINT:   "⏰ Reclamación sin atender",
        t.ACTION_FOLLOWUP:    "⏰ Devolver llamada pendiente",
    }
    title = title_by_type.get(t.action_type, "⏰ Acción pendiente")
    send_push(
        restaurant=t.restaurant,
        title=title,
        body=f"{_caller_label(t)} lleva 15 min esperando.",
        url=f"/portal/{t.restaurant.slug}/r/{t.token}/",
        urgency="high",
        tag=f"escalation-l1-{t.pk}",
    )
    logger.info("escalation L1: reminder push | token=%s restaurant=%s",
                t.token[:12], t.restaurant.slug)


def _fire_auto_sms(t: CallActionToken, send_sms) -> None:
    """L2: auto-SMS to caller + push to owner. SMS only sent if we have a phone."""
    # Owner-facing push
    send_push(
        restaurant=t.restaurant,
        title="🚨 SMS automático enviado al cliente",
        body=f"{_caller_label(t)} fue notificado automáticamente.",
        url=f"/portal/{t.restaurant.slug}/r/{t.token}/",
        urgency="high",
        tag=f"escalation-l2-{t.pk}",
    )

    # Caller-facing auto-SMS (background — same pattern as call_action_respond)
    phone = t.call_detail.caller_phone or ""
    if phone:
        msg = (
            f"Hola, recibimos tu llamada a {t.restaurant.name} y "
            f"nos pondremos en contacto contigo dentro de una hora.\n\n"
            f"(Mensaje automatizado. Por favor no responder por SMS — "
            f"llámanos si es urgente.)"
        )
        threading.Thread(
            target=send_sms, args=(t.restaurant, phone, msg), daemon=True
        ).start()
        logger.info("escalation L2: auto-SMS → %s | token=%s restaurant=%s",
                    phone, t.token[:12], t.restaurant.slug)
    else:
        logger.warning("escalation L2: no caller phone, SMS skipped | token=%s", t.token[:12])


def _fire_final_alert(t: CallActionToken,
                      send_reservation, send_complaint, send_followup) -> None:
    """L3: re-send the owner alert email (last-resort). Dashboard pulse is CSS-driven."""
    sender = {
        t.ACTION_RESERVATION: send_reservation,
        t.ACTION_COMPLAINT:   send_complaint,
        t.ACTION_FOLLOWUP:    send_followup,
    }.get(t.action_type)

    if not sender:
        logger.info("escalation L3: no email sender for action_type=%s | token=%s",
                    t.action_type, t.token[:12])
        return

    try:
        sender(t.call_detail.call_event, t.restaurant)
        logger.info("escalation L3: final email re-sent | token=%s restaurant=%s",
                    t.token[:12], t.restaurant.slug)
    except Exception:
        logger.exception("escalation L3: email re-send failed | token=%s", t.token[:12])
