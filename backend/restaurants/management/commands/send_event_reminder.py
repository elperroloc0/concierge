"""
Management command: send_event_reminder

Push + email reminder to refresh the entertainment & events schedule, so the
agent never serves stale event info. Opt-in per restaurant (notify_event_reminder)
and fires on the owner-chosen weekday (event_reminder_weekday, restaurant timezone).

Runs daily (chained on the daily-tasks cron); the per-restaurant weekday gate
decides who actually gets a reminder each day.

    manage.py send_event_reminder
    manage.py send_event_reminder --force                 # ignore the weekday gate
    manage.py send_event_reminder --restaurant <slug>     # limit to one restaurant
"""
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string

from restaurants.models import Restaurant
from restaurants.push import send_push

logger = logging.getLogger(__name__)


def _tz(restaurant):
    try:
        return ZoneInfo(restaurant.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _count_upcoming_events(restaurant) -> int:
    """How many future-dated structured events are on file (for a useful message)."""
    kb = getattr(restaurant, "knowledge_base", None)
    if not kb:
        return 0
    raw = kb.special_events if isinstance(kb.special_events, list) else []
    today = datetime.now(tz=_tz(restaurant)).date()
    n = 0
    for e in raw:
        if not isinstance(e, dict):
            continue
        ds = (e.get("date") or "").strip()
        if not ds or not (e.get("description") or "").strip():
            continue
        try:
            if date.fromisoformat(ds) >= today:
                n += 1
        except ValueError:
            continue
    return n


class Command(BaseCommand):
    help = "Push + email reminder to refresh the entertainment & events schedule (per configured weekday)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Ignore the weekday gate (send to all opted-in).")
        parser.add_argument("--restaurant", help="Limit to a single restaurant slug.")

    def handle(self, *args, **options):
        qs = Restaurant.objects.filter(is_active=True, notify_event_reminder=True)
        if options.get("restaurant"):
            qs = qs.filter(slug=options["restaurant"])

        sent = 0
        for restaurant in qs:
            today_wd = datetime.now(tz=_tz(restaurant)).weekday()
            if not options.get("force") and today_wd != restaurant.event_reminder_weekday:
                continue

            upcoming = _count_upcoming_events(restaurant)
            kb_url = f"/portal/{restaurant.slug}/knowledge-base/"
            if upcoming == 0:
                body = ("No upcoming events on file — add this week's live-music lineup and any "
                        "dated events so callers hear what's current.")
            else:
                body = (f"{upcoming} upcoming event(s) on file. Review the schedule and add what's "
                        "next so callers always get current info.")

            # ── Push (best-effort) ──
            try:
                send_push(
                    restaurant=restaurant,
                    title="🎵 Update your entertainment & events",
                    body=body,
                    url=kb_url,
                    urgency="normal",
                    tag="event-reminder",
                )
            except Exception:
                logger.exception("event_reminder: push failed | restaurant=%s", restaurant.slug)

            # ── Email (only when email notifications are on and an address exists) ──
            notify_email = restaurant.notify_email
            if restaurant.notify_via_email and notify_email:
                try:
                    html_body = render_to_string("emails/event_reminder.html", {
                        "restaurant_name": restaurant.name,
                        "body":            body,
                        "upcoming":        upcoming,
                        "kb_url":          kb_url,
                    })
                    text_body = f"{body}\n\nUpdate now: {kb_url}"
                    msg = EmailMultiAlternatives(
                        f"🎵 Update your entertainment & events — {restaurant.name}",
                        text_body, from_email=None, to=[notify_email],
                    )
                    msg.attach_alternative(html_body, "text/html")
                    msg.send()
                except Exception:
                    logger.exception("event_reminder: email failed | restaurant=%s", restaurant.slug)

            sent += 1
            logger.info("event_reminder: sent | restaurant=%s | upcoming=%d", restaurant.slug, upcoming)
            self.stdout.write(self.style.SUCCESS(f"✓ {restaurant.name} → reminder (upcoming={upcoming})"))

        self.stdout.write(f"event_reminder: {sent} reminder(s) sent")
