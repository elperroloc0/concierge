from django.db import migrations
from django.db.models import Count, Max


def backfill_caller_memory(apps, schema_editor):
    """
    Populate CallerMemory from existing CallDetail records.
    For each unique (restaurant, caller_phone) creates a CallerMemory
    using aggregate data. Does not overwrite existing preferences/staff_notes.
    """
    CallDetail   = apps.get_model("restaurants", "CallDetail")
    CallerMemory = apps.get_model("restaurants", "CallerMemory")
    Restaurant   = apps.get_model("restaurants", "Restaurant")

    created_count = updated_count = skipped_count = 0

    for restaurant in Restaurant.objects.all():
        phone_stats = (
            CallDetail.objects
            .filter(call_event__restaurant=restaurant)
            .exclude(caller_phone="")
            .values("caller_phone")
            .annotate(total_calls=Count("id"), last_call=Max("created_at"))
        )

        for row in phone_stats:
            phone      = row["caller_phone"]
            call_count = row["total_calls"]
            last_call  = row["last_call"]

            # Most recent record for name, email, summary, call_reason
            latest = (
                CallDetail.objects
                .filter(call_event__restaurant=restaurant, caller_phone=phone)
                .order_by("-created_at")
                .first()
            )
            if not latest:
                skipped_count += 1
                continue

            name        = (latest.caller_name  or "").strip()
            email       = (latest.caller_email or "").strip()
            summary     = (latest.call_summary or "").strip()
            caller_type = "business" if latest.call_reason == "non_customer" else "guest"

            mem, created = CallerMemory.objects.get_or_create(
                phone=phone,
                restaurant=restaurant,
                defaults={
                    "name":              name,
                    "email":             email,
                    "caller_type":       caller_type,
                    "call_count":        call_count,
                    "last_call_at":      last_call,
                    "last_call_summary": summary,
                },
            )

            if created:
                created_count += 1
            else:
                # Already exists — update history fields only,
                # never touch preferences or staff_notes.
                changed = []
                if name and not mem.name:
                    mem.name = name
                    changed.append("name")
                if email and not mem.email:
                    mem.email = email
                    changed.append("email")
                if call_count > mem.call_count:
                    mem.call_count = call_count
                    changed.append("call_count")
                if last_call and (not mem.last_call_at or last_call > mem.last_call_at):
                    mem.last_call_at = last_call
                    changed.append("last_call_at")
                if summary and not mem.last_call_summary:
                    mem.last_call_summary = summary
                    changed.append("last_call_summary")
                if changed:
                    mem.save(update_fields=changed)
                    updated_count += 1

    print(
        f"\nbackfill_caller_memory: "
        f"created={created_count} updated={updated_count} skipped={skipped_count}"
    )


def reverse_backfill(apps, schema_editor):
    # Safe no-op: don't delete CallerMemory on rollback —
    # records may have been manually edited or added by live calls.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0047_caller_memory_caller_type"),
    ]

    operations = [
        migrations.RunPython(backfill_caller_memory, reverse_backfill),
    ]
