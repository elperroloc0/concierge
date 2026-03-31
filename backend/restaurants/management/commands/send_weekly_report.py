"""
Management command: send_weekly_report

Generates a weekly call quality report for each active restaurant using:
1. Aggregated call_signals from Retell post-call analysis
2. Claude API to produce an owner narrative + prompt improvement suggestions

Schedule with cron to run every Monday at 8am:
    0 8 * * 1  python manage.py send_weekly_report

Flags:
    --week YYYY-MM-DD   Use a specific week_start (default: last Monday)
    --dry-run           Print to stdout, skip DB save and email
    --force             Overwrite existing WeeklyReport if it exists
    --prompt-only       Save to DB but skip email to owner
    --restaurant <slug> Limit to a single restaurant
"""
import json
import logging
import os
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string

from restaurants.models import CallDetail, Restaurant, WeeklyReport

logger = logging.getLogger(__name__)

# ─── Claude prompts ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """Eres un analista especializado en agentes de voz para restaurantes.
Recibes el análisis agregado de una semana de llamadas: señales de calidad estructuradas
extraídas por Retell sobre cada llamada, más resúmenes de las llamadas más relevantes.

Debes producir DOS outputs separados por los delimitadores exactos indicados.

===OWNER===
Análisis ejecutivo para el dueño del restaurante.
Tono: analista de negocio que conoce bien la industria — directo, sin relleno, accionable.
No es un resumen de métricas, es interpretación de lo que significan para el negocio.
Escribe en el idioma indicado en los datos (weekly_report_language).
Estructura obligatoria:
  Visión general
  Reservas
  Fricción y fallos del agente
  Escalaciones (omitir sección si no hubo transferencias)
  Recomendaciones (máximo 3, concretas y priorizadas)

===PROMPT===
Análisis técnico para el desarrollador del sistema.
Para cada fallo detectado: problema → causa probable → texto concreto a añadir o
cambiar en el prompt del agente o en el knowledge base.
Formato: ### N. [Título del problema] [N llamadas afectadas — PRIORIDAD]
Prioriza por impacto (cuántas llamadas afectó).
Incluye citas exactas de unanswered_question y agent_response_to_unanswered cuando estén disponibles."""


# ─── Aggregation ──────────────────────────────────────────────────────────────

def aggregate_metrics(restaurant: Restaurant, week_start: date, week_end: date) -> dict:
    """Aggregate CallDetail signals for the given week into a structured dict."""
    details = list(
        CallDetail.objects.filter(
            call_event__restaurant=restaurant,
            call_event__created_at__date__gte=week_start,
            call_event__created_at__date__lt=week_end,
        ).select_related("call_event")
    )

    if not details:
        return {}

    real    = [d for d in details if not d.is_spam]
    spam    = [d for d in details if d.is_spam]

    # Quality breakdown — iterate in Python for SQLite compatibility
    quality_counts: Counter = Counter()
    agent_failures = []
    unanswered_questions = []
    confusion_moments = []
    frustration_count = 0
    lang_inconsistency_count = 0
    unnecessary_transfers = 0

    for d in real:
        sig = d.call_signals or {}
        q = sig.get("call_quality")
        if q:
            quality_counts[q] += 1

        if sig.get("agent_failed_to_answer"):
            uq = (sig.get("unanswered_question") or "").strip()
            ar = (sig.get("agent_response_to_unanswered") or "").strip()
            agent_failures.append({"question": uq, "agent_response": ar})
            if uq:
                unanswered_questions.append(uq)

        cm = (sig.get("agent_confusion_moment") or "").strip()
        if cm:
            confusion_moments.append(cm)

        if sig.get("caller_frustration"):
            frustration_count += 1

        if sig.get("language_consistency") is False:
            lang_inconsistency_count += 1

        if sig.get("transfer_was_necessary") is False:
            unnecessary_transfers += 1

    # Duration average
    durations = [d.duration_seconds for d in details if d.duration_seconds is not None]
    avg_duration = round(sum(durations) / len(durations)) if durations else None

    # Reason and sentiment breakdowns
    reason_breakdown = dict(Counter(d.call_reason for d in real if d.call_reason))
    sentiment_breakdown = dict(Counter(d.caller_sentiment for d in real if d.caller_sentiment))

    return {
        "total_calls":    len(details),
        "real_calls":     len(real),
        "spam_calls":     len(spam),
        "avg_duration_seconds": avg_duration,
        "reservations":   sum(1 for d in real if d.wants_reservation),
        "complaints":     sum(1 for d in real if d.call_reason == "complaint"),
        "follow_ups":     sum(1 for d in real if d.follow_up_needed),
        "reason_breakdown":    reason_breakdown,
        "sentiment_breakdown": sentiment_breakdown,
        "call_quality":        dict(quality_counts),
        "agent_failures": {
            "total":    len(agent_failures),
            "examples": agent_failures[:10],
        },
        "unanswered_questions":    unanswered_questions[:10],
        "confusion_moments":       confusion_moments[:5],
        "caller_frustration":      frustration_count,
        "language_inconsistencies": lang_inconsistency_count,
        "unnecessary_transfers":   unnecessary_transfers,
    }


def select_relevant_summaries(restaurant: Restaurant, week_start: date, week_end: date,
                               max_items: int = 8) -> list:
    """Return up to max_items call_summary strings prioritized by diagnostic value."""
    base_qs = CallDetail.objects.filter(
        call_event__restaurant=restaurant,
        call_event__created_at__date__gte=week_start,
        call_event__created_at__date__lt=week_end,
        is_spam=False,
    ).exclude(call_summary="")

    seen, selected = set(), []

    # Priority order: agent failures → frustration → poor quality → recent
    priority_filters = [
        {"call_signals__agent_failed_to_answer": True},
        {"call_signals__caller_frustration": True},
        {"call_signals__call_quality": "poor"},
    ]

    for filt in priority_filters:
        for d in base_qs.filter(**filt).order_by("-call_event__created_at"):
            if d.pk not in seen and d.call_summary:
                selected.append(d.call_summary)
                seen.add(d.pk)
                if len(selected) >= max_items:
                    return selected

    # Fill remaining slots with most recent calls
    for d in base_qs.order_by("-call_event__created_at"):
        if d.pk not in seen and d.call_summary:
            selected.append(d.call_summary)
            seen.add(d.pk)
            if len(selected) >= max_items:
                break

    return selected


# ─── Claude generation ────────────────────────────────────────────────────────

def generate_report(restaurant: Restaurant, metrics: dict, summaries: list,
                    week_start: date, week_end: date) -> tuple:
    """
    Call Claude API and return (owner_summary, prompt_suggestions, model_used, generation_cost).
    """
    import anthropic

    language = restaurant.weekly_report_language or "es"
    lang_label = "Spanish" if language == "es" else "English"

    user_prompt = (
        f"Restaurant: {restaurant.name}\n"
        f"Week: {week_start} to {week_end}\n"
        f"weekly_report_language: {lang_label}\n\n"
        f"--- METRICS ---\n"
        f"{json.dumps(metrics, indent=2, ensure_ascii=False)}\n\n"
        f"--- RELEVANT CALL SUMMARIES (from Retell) ---\n"
        + "\n".join(f"- {s}" for s in summaries)
    )

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text

    if "===PROMPT===" not in raw:
        logger.warning(
            "generate_report: missing ===PROMPT=== delimiter in Claude output | restaurant=%s",
            restaurant.slug,
        )
        owner_summary = raw.replace("===OWNER===", "").strip()
        prompt_suggestions = ""
    else:
        owner_summary, _, prompt_suggestions = raw.partition("===PROMPT===")
        owner_summary = owner_summary.replace("===OWNER===", "").strip()
        prompt_suggestions = prompt_suggestions.strip()

    model_used = response.model
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    # claude-sonnet-4-6 pricing: $3/MTok in, $15/MTok out
    cost = Decimal(str(round(input_tokens * 3 / 1_000_000 + output_tokens * 15 / 1_000_000, 6)))

    return owner_summary, prompt_suggestions, model_used, cost


# ─── Management command ───────────────────────────────────────────────────────

class Command(BaseCommand):
    help = "Generate and email the weekly call quality report for each active restaurant."

    def add_arguments(self, parser):
        parser.add_argument(
            "--week", type=str, default=None,
            help="week_start as YYYY-MM-DD (default: last Monday)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print report to stdout. Skip DB save and email.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Overwrite existing WeeklyReport for this week.",
        )
        parser.add_argument(
            "--prompt-only", action="store_true",
            help="Save to DB but skip email to owner.",
        )
        parser.add_argument(
            "--restaurant", type=str, default=None,
            help="Limit to a single restaurant by slug.",
        )

    def handle(self, *args, **options):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise CommandError("ANTHROPIC_API_KEY is not set. Cannot generate reports.")

        # Compute week window
        if options["week"]:
            try:
                week_start = date.fromisoformat(options["week"])
            except ValueError:
                raise CommandError(f"Invalid date format: {options['week']}. Use YYYY-MM-DD.")
        else:
            today = date.today()
            # Last Monday (works correctly on any day of the week)
            week_start = today - timedelta(days=today.weekday() + 7)
        week_end = week_start + timedelta(days=7)

        self.stdout.write(f"Weekly report | week {week_start} → {week_end}")

        # Build restaurant queryset
        qs = Restaurant.objects.filter(is_active=True, notify_weekly_report=True)
        if options["restaurant"]:
            qs = qs.filter(slug=options["restaurant"])
            if not qs.exists():
                raise CommandError(f"Restaurant not found: {options['restaurant']}")

        for restaurant in qs:
            self._process(restaurant, week_start, week_end, options)

    def _process(self, restaurant: Restaurant, week_start: date, week_end: date, options: dict):
        dry_run      = options["dry_run"]
        force        = options["force"]
        prompt_only  = options["prompt_only"]

        # Check for existing report
        existing = WeeklyReport.objects.filter(
            restaurant=restaurant, week_start=week_start
        ).first()
        if existing and not force:
            self.stdout.write(
                f"  skip {restaurant.slug} — report exists (use --force to overwrite)"
            )
            return

        metrics = aggregate_metrics(restaurant, week_start, week_end)
        if not metrics:
            self.stdout.write(f"  skip {restaurant.slug} — no calls this week")
            return

        summaries = select_relevant_summaries(restaurant, week_start, week_end)

        try:
            owner_summary, prompt_suggestions, model_used, cost = generate_report(
                restaurant, metrics, summaries, week_start, week_end,
            )
        except Exception:
            logger.exception(
                "send_weekly_report: Claude generation failed | restaurant=%s", restaurant.slug
            )
            self.stdout.write(self.style.ERROR(f"  ✗ {restaurant.slug} — Claude API failed"))
            return

        if dry_run:
            self.stdout.write(f"\n{'='*60}\n{restaurant.name} | {week_start}\n{'='*60}")
            self.stdout.write(f"\nMETRICS:\n{json.dumps(metrics, indent=2, ensure_ascii=False)}")
            self.stdout.write(f"\n--- OWNER SUMMARY ---\n{owner_summary}")
            self.stdout.write(f"\n--- PROMPT SUGGESTIONS ---\n{prompt_suggestions}")
            self.stdout.write(f"\nmodel={model_used} | cost=${cost}")
            return

        # Save or update report
        report, _ = WeeklyReport.objects.update_or_create(
            restaurant=restaurant,
            week_start=week_start,
            defaults={
                "week_end":           week_end,
                "metrics":            metrics,
                "owner_summary":      owner_summary,
                "prompt_suggestions": prompt_suggestions,
                "model_used":         model_used,
                "generation_cost":    cost,
            },
        )

        if prompt_only:
            self.stdout.write(
                self.style.SUCCESS(f"  ✓ {restaurant.slug} — saved (no email, --prompt-only)")
            )
            return

        self._send_email(restaurant, report)

    def _send_email(self, restaurant: Restaurant, report: WeeklyReport):
        notify_email = restaurant.notify_email or (
            restaurant.user.email if restaurant.user else ""
        )
        if not notify_email:
            logger.warning(
                "send_weekly_report: no notify_email | restaurant=%s", restaurant.slug
            )
            return

        from django.conf import settings as django_settings

        base_url = getattr(django_settings, "RETELL_WEBHOOK_BASE_URL", "") or "http://localhost:8000"
        portal_url  = f"{base_url}/portal/{restaurant.slug}/reports/{report.pk}/"
        calls_url   = f"{base_url}/portal/{restaurant.slug}/calls/?follow_up=true"

        metrics = report.metrics
        week_start_str = report.week_start.strftime("%-d %b %Y")
        week_end_str   = (report.week_end - timedelta(days=1)).strftime("%-d %b %Y")

        if restaurant.weekly_report_language == "es":
            subject = f"Reporte Semanal — {restaurant.name} — semana del {week_start_str}"
        else:
            subject = f"Weekly Report — {restaurant.name} — week of {week_start_str}"

        text_body = (
            f"Reporte Semanal — {restaurant.name}\n"
            f"Semana del {week_start_str} al {week_end_str}\n\n"
            f"LLAMADAS\n"
            f"  Total: {metrics.get('total_calls', 0)}  |  "
            f"Reales: {metrics.get('real_calls', 0)}  |  "
            f"Spam: {metrics.get('spam_calls', 0)}\n\n"
            f"RESERVAS\n"
            f"  Solicitadas: {metrics.get('reservations', 0)}\n\n"
            f"CALIDAD DEL AGENTE\n"
            f"  {metrics.get('call_quality', {})}\n"
            f"  Fallos de información: {metrics.get('agent_failures', {}).get('total', 0)}\n\n"
            f"---\n\n"
            f"{report.owner_summary}\n\n"
            f"---\n"
            f"Ver reporte completo: {portal_url}\n"
            f"Llamadas pendientes:  {calls_url}\n"
        )

        html_body = render_to_string("emails/weekly_report.html", {
            "restaurant_name": restaurant.name,
            "week_start_str":  week_start_str,
            "week_end_str":    week_end_str,
            "metrics":         metrics,
            "owner_summary":   report.owner_summary,
            "portal_url":      portal_url,
            "calls_url":       calls_url,
        })

        try:
            from django.conf import settings as django_settings
            msg = EmailMultiAlternatives(
                subject, text_body,
                from_email=None,
                to=[notify_email],
            )
            msg.attach_alternative(html_body, "text/html")
            msg.send()
            logger.info(
                "send_weekly_report: sent to %s | restaurant=%s | calls=%d",
                notify_email, restaurant.slug, metrics.get("total_calls", 0),
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  ✓ {restaurant.name} ({metrics.get('total_calls', 0)} calls) → {notify_email}"
                )
            )
        except Exception:
            logger.exception("send_weekly_report: email failed | restaurant=%s", restaurant.slug)
            self.stdout.write(self.style.ERROR(f"  ✗ {restaurant.slug} — email failed"))
