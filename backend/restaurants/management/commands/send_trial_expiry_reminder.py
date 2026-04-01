"""
Management command: send_trial_expiry_reminder

Sends a reminder email to restaurants whose free trial expires in exactly
3 days and have not yet configured a paid subscription.

Schedule with cron to run once daily (e.g. 09:00):
    manage.py send_trial_expiry_reminder
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.utils import timezone

from restaurants.models import Restaurant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Email trial restaurants whose subscription expires in 3 days."

    def handle(self, *args, **options):
        now = timezone.now()
        window_start = now + timedelta(days=3)
        window_end   = now + timedelta(days=4)

        restaurants = Restaurant.objects.filter(
            is_active=True,
            notify_via_email=True,
            subscription__status="trialing",
            subscription__current_period_end__gte=window_start,
            subscription__current_period_end__lt=window_end,
        ).exclude(notify_email="").select_related("subscription")

        if not restaurants.exists():
            self.stdout.write("No trial reminders to send today.")
            return

        base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"

        for restaurant in restaurants:
            sub         = restaurant.subscription
            expiry_date = sub.current_period_end
            days_left   = (expiry_date.date() - now.date()).days
            billing_url = f"{base_url}/portal/{restaurant.slug}/billing/"

            ctx = {
                "restaurant_name": restaurant.name,
                "expiry_date":     expiry_date,
                "days_left":       days_left,
                "billing_url":     billing_url,
            }

            html_body = render_to_string("emails/trial_expiry_reminder.html", ctx)
            text_body = (
                f"Tu período de prueba gratuito vence el {expiry_date.strftime('%d de %B de %Y')}.\n\n"
                "Para que tu agente de IA siga contestando llamadas, activa tu suscripción ahora.\n\n"
                f"Configurar suscripción: {billing_url}\n"
            )
            subject = f"⏳ Tu prueba gratuita vence en {days_left} días — {restaurant.name}"

            try:
                msg = EmailMultiAlternatives(
                    subject, text_body,
                    from_email=None,  # uses DEFAULT_FROM_EMAIL
                    to=[restaurant.notify_email],
                )
                msg.attach_alternative(html_body, "text/html")
                msg.send()
                logger.info(
                    "trial_expiry_reminder: sent | restaurant=%s | expiry=%s",
                    restaurant.slug, expiry_date.date(),
                )
                self.stdout.write(self.style.SUCCESS(
                    f"✓ {restaurant.name} → {restaurant.notify_email} (vence {expiry_date.date()})"
                ))
            except Exception:
                logger.exception("trial_expiry_reminder: failed | restaurant=%s", restaurant.slug)
                self.stdout.write(self.style.ERROR(f"✗ {restaurant.name} — email failed"))
