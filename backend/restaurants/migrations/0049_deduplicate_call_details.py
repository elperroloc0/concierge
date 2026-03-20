from django.db import migrations


PRIORITY = {"call_analyzed": 0, "call_ended": 1, "call_in_progress": 2, "call_started": 3}


def deduplicate_call_details(apps, schema_editor):
    """
    For calls where multiple events each have a CallDetail, keep only the best one.
    Priority: call_analyzed > call_ended > call_in_progress > call_started
    """
    CallDetail = apps.get_model("restaurants", "CallDetail")
    CallEvent  = apps.get_model("restaurants", "CallEvent")

    # Group CallDetails by call_id
    best_by_call_id = {}   # call_id -> (detail_pk, priority)
    dupes_to_delete = []

    for detail in CallDetail.objects.select_related("call_event").iterator():
        call_id = (
            detail.call_event.payload.get("call", {}).get("call_id", "")
            if detail.call_event and detail.call_event.payload
            else ""
        )
        if not call_id:
            continue

        event_type = detail.call_event.event_type if detail.call_event else ""
        priority   = PRIORITY.get(event_type, 99)

        if call_id not in best_by_call_id:
            best_by_call_id[call_id] = (detail.pk, priority)
        else:
            existing_pk, existing_priority = best_by_call_id[call_id]
            if priority < existing_priority:
                dupes_to_delete.append(existing_pk)
                best_by_call_id[call_id] = (detail.pk, priority)
            else:
                dupes_to_delete.append(detail.pk)

    if dupes_to_delete:
        deleted, _ = CallDetail.objects.filter(pk__in=dupes_to_delete).delete()
        print(f"\ndeduplicate_call_details: deleted {deleted} duplicate CallDetail records")
    else:
        print("\ndeduplicate_call_details: no duplicates found")


def reverse_dedup(apps, schema_editor):
    # Data is gone after dedup — safe no-op on rollback.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0048_backfill_caller_memory"),
    ]

    operations = [
        migrations.RunPython(deduplicate_call_details, reverse_dedup),
    ]
