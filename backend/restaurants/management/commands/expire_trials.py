"""
Management command: expire_trials

Finds DB-only trials (no Stripe subscription) whose current_period_end has
passed and transitions them to 'inactive'.  Sends a notification email to
the restaurant owner.

Schedule with cron to run once daily (e.g. 09:05, after send_trial_expiry_reminder):
    manage.py expire_trials
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.utils import timezone

from restaurants.models import Subscription

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Deactivate DB-only trials whose period has ended and notify owners."

    def handle(self, *args, **options):
        now = timezone.now()

        expired = Subscription.objects.filter(
            status="trialing",
            current_period_end__lt=now,
            stripe_subscription_id="",  # DB-only trial — Stripe trials are handled by Stripe webhooks
        ).select_related("restaurant")

        if not expired.exists():
            self.stdout.write("No expired trials found.")
            return

        base_url = settings.RETELL_WEBHOOK_BASE_URL or "http://localhost:8000"

        for sub in expired:
            restaurant = sub.restaurant
            sub.status = "inactive"
            sub.save(update_fields=["status"])
            logger.info("expire_trials: %s → inactive", restaurant.slug)

            # Send notification email
            if restaurant.notify_via_email and restaurant.notify_email:
                billing_url = f"{base_url}/portal/{restaurant.slug}/billing/"
                ctx = {
                    "restaurant_name": restaurant.name,
                    "billing_url": billing_url,
                }

                html_body = render_to_string("emails/trial_expired.html", ctx)
                text_body = (
                    f"El período de prueba gratuito de {restaurant.name} ha finalizado.\n\n"
                    "Tu agente de IA ya no está contestando llamadas. "
                    "Para reactivar el servicio, activa tu suscripción.\n\n"
                    f"Activar suscripción: {billing_url}\n"
                )

                try:
                    msg = EmailMultiAlternatives(
                        f"Tu prueba gratuita ha finalizado — {restaurant.name}",
                        text_body,
                        from_email=None,
                        to=[restaurant.notify_email],
                    )
                    msg.attach_alternative(html_body, "text/html")
                    msg.send()
                    self.stdout.write(self.style.SUCCESS(
                        f"  {restaurant.name} → inactive, email sent to {restaurant.notify_email}"
                    ))
                except Exception:
                    logger.exception("expire_trials: email failed | restaurant=%s", restaurant.slug)
                    self.stdout.write(self.style.ERROR(
                        f"  {restaurant.name} → inactive, email FAILED"
                    ))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"  {restaurant.name} → inactive (no email configured)"
                ))
