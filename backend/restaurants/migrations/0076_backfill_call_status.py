"""Backfill CallDetail.status from legacy flags.

Rules (in order — first match wins):

  1. is_spam=True                             → resolved
  2. follow_up_needed=False                   → resolved
  3. older than 5 days                        → resolved
  4. follow_up_needed=True AND recent AND
     (complaint OR needs_review OR
      pending reservation lead)               → needs_reply
  5. anything else                            → new

Data audit on prod (712 rows total) before writing this migration:
  needs_reply ≈ 3, new ≈ 0, resolved ≈ 709. Old `follow_up_needed=True`
  rows older than 5 days (≈224) are intentionally demoted to `resolved`
  rather than flooding `needs_reply` — operators historically never
  closed calls explicitly, so the flag carries little signal at that age.
  Overdue tracking starts fresh from day one of the new system.

`first_viewed_at` stays NULL for all rows — existing inbox calls will
appear "unread" once, which is informative rather than noisy.
"""
from datetime import timedelta

from django.db import migrations


def backfill_status(apps, schema_editor):
    CallDetail = apps.get_model("restaurants", "CallDetail")
    from django.utils import timezone

    cutoff = timezone.now() - timedelta(days=5)

    # 1+2+3: resolved bucket — single UPDATE
    CallDetail.objects.filter(
        is_spam=True,
    ).update(status="resolved")

    CallDetail.objects.filter(
        is_spam=False,
        follow_up_needed=False,
    ).update(status="resolved")

    CallDetail.objects.filter(
        is_spam=False,
        follow_up_needed=True,
        created_at__lt=cutoff,
    ).update(status="resolved")

    # 4: needs_reply — recent + flagged + signal
    from django.db.models import Q
    CallDetail.objects.filter(
        is_spam=False,
        follow_up_needed=True,
        created_at__gte=cutoff,
    ).filter(
        Q(call_reason="complaint")
        | Q(needs_review=True)
        | Q(wants_reservation=True, reservation_status="pending")
    ).update(status="needs_reply")

    # 5: leftover rows already default to "new" via AddField default,
    #    so no explicit UPDATE needed.


def reverse(apps, schema_editor):
    # Reset all rows to the model default — schema migration removal handles
    # the rest. Safe to re-run forward after rollback.
    CallDetail = apps.get_model("restaurants", "CallDetail")
    CallDetail.objects.update(status="new")


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0075_add_call_status_and_first_viewed"),
    ]

    operations = [
        migrations.RunPython(backfill_status, reverse),
    ]
