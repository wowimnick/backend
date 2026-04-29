"""
Enqueue Gemini description formatting for all matching classes, bypassing the
ready+hash short-circuit (always regenerates summary + sections when description is non-empty).

Requires Celery workers running format_class_description_task.
"""

import time

from django.core.management.base import BaseCommand

from quickstart.models import ClassesMain
from quickstart.tasks.business_tasks import format_class_description_task


class Command(BaseCommand):
    help = (
        "Enqueue Celery tasks to regenerate description_summary / description_sections "
        "via Gemini for every matching class, even if already ready with the same hash."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max classes to enqueue (0 = no limit).",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="Seconds to sleep between enqueue calls (rate limiting).",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Include classes where status is not 'active' (default: active only).",
        )
        parser.add_argument(
            "--include-empty-description",
            action="store_true",
            help="Include rows with blank description (task clears AI fields; no Gemini call).",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        delay = max(0.0, options["delay"])
        include_inactive = options["include_inactive"]
        include_empty = options["include_empty_description"]

        qs = ClassesMain.objects.all()
        if not include_inactive:
            qs = qs.filter(status="active")
        if not include_empty:
            qs = qs.exclude(description__exact="")

        qs = qs.order_by("classId")
        if limit > 0:
            qs = qs[:limit]

        ids = list(qs.values_list("classId", flat=True))
        self.stdout.write(
            f"Enqueueing {len(ids)} forced description format task(s) (force=True)."
        )

        for i, pk in enumerate(ids):
            ClassesMain.objects.filter(pk=pk).update(description_ai_status="pending")
            format_class_description_task.delay(pk, force=True)
            if delay and i < len(ids) - 1:
                time.sleep(delay)

        self.stdout.write(self.style.SUCCESS("Done."))
