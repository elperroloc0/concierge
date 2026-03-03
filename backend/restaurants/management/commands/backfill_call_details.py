from django.core.management.base import BaseCommand

from restaurants.models import CallEvent
from restaurants.views import _build_call_detail_from_payload


class Command(BaseCommand):
    help = "Backfill CallDetail records for existing call_ended CallEvents that don't have one yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many records would be created without creating them.",
        )

    def handle(self, *args, **options):
        qs = (
            CallEvent.objects
            .filter(event_type="call_ended")
            .exclude(detail__isnull=False)
            .order_by("created_at")
        )

        total = qs.count()
        self.stdout.write(f"Found {total} call_ended events without CallDetail.")

        if options["dry_run"]:
            self.stdout.write("Dry run — no changes made.")
            return

        success, failed = 0, 0
        for event in qs.iterator():
            try:
                _build_call_detail_from_payload(event)
                success += 1
            except Exception as e:
                self.stderr.write(f"  FAILED pk={event.pk}: {e}")
                failed += 1

        self.stdout.write(
            self.style.SUCCESS(f"Done. Created: {success}, Failed: {failed}")
        )
