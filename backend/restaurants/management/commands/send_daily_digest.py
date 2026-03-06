"""
Management command: send_daily_digest

Sends a morning email to each restaurant owner with a summary
of the previous day's calls.

Schedule with cron or Celery Beat to run once daily (e.g. 08:00):
    manage.py send_daily_digest
"""
import logging
from datetime import timedelta

from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.utils import timezone

from restaurants.models import CallDetail, CallEvent, Restaurant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Email each restaurant owner a summary of yesterday's calls."

    def handle(self, *args, **options):
        now   = timezone.now()
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end   = start + timedelta(days=1)

        restaurants = Restaurant.objects.filter(
            is_active=True,
            notify_via_email=True,
            notify_daily_digest=True,
        ).exclude(notify_email="")

        for restaurant in restaurants:
            events = CallEvent.objects.filter(
                restaurant=restaurant,
                event_type="call_ended",
                created_at__gte=start,
                created_at__lt=end,
            )
            total = events.count()
            if total == 0:
                continue

            # Aggregate stats
            details = CallDetail.objects.filter(call_event__in=events)
            reservations = details.filter(wants_reservation=True).count()
            complaints   = details.filter(call_reason="complaint").count()
            follow_ups   = details.filter(follow_up_needed=True).count()

            # Reason breakdown
            from collections import Counter
            reasons = Counter(details.values_list("call_reason", flat=True))
            reason_lines = [f"  • {reason}: {count}" for reason, count in reasons.most_common()]

            date_str = start.strftime("%A, %B %-d, %Y")

            text_body = (
                f"📊 Daily Call Digest — {restaurant.name}\n"
                f"Date: {date_str}\n\n"
                f"Total calls: {total}\n"
                f"Reservation requests: {reservations}\n"
                f"Complaints: {complaints}\n"
                f"Follow-ups pending: {follow_ups}\n\n"
                f"By reason:\n" + "\n".join(reason_lines) + "\n"
            )

            html_body = render_to_string("emails/daily_digest.html", {
                "restaurant_name": restaurant.name,
                "date_str":        date_str,
                "total":           total,
                "reservations":    reservations,
                "complaints":      complaints,
                "follow_ups":      follow_ups,
                "reason_lines":    reasons.most_common(),
                "portal_url":      f"/portal/{restaurant.slug}/calls/",
            })

            subject = f"📊 Daily Digest — {restaurant.name} — {date_str}"
            notify_email = restaurant.notify_email

            try:
                msg = EmailMultiAlternatives(
                    subject, text_body,
                    from_email=None,  # uses DEFAULT_FROM_EMAIL
                    to=[notify_email],
                )
                msg.attach_alternative(html_body, "text/html")
                msg.send()
                logger.info("daily_digest: sent to %s | restaurant=%s | calls=%d",
                            notify_email, restaurant.slug, total)
                self.stdout.write(self.style.SUCCESS(f"✓ {restaurant.name} ({total} calls) → {notify_email}"))
            except Exception:
                logger.exception("daily_digest: failed | restaurant=%s", restaurant.slug)
                self.stdout.write(self.style.ERROR(f"✗ {restaurant.name} — email failed"))
