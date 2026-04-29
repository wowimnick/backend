"""
Enqueue Gemini description formatting for active classes (optional rate limit between tasks).
"""
import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from quickstart.models import ClassesMain
from quickstart.tasks.business_tasks import format_class_description_task


class Command(BaseCommand):
    help = (
        "Enqueue Celery tasks to generate description_summary / description_sections "
        "via Gemini for active classes."
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
            "--force",
            action="store_true",
            help="Include classes already in description_ai_status=ready.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        delay = max(0.0, options["delay"])
        force = options["force"]

        qs = ClassesMain.objects.filter(status="active").exclude(description__exact="")
        if not force:
            qs = qs.filter(
                Q(description_ai_status__in=["stale", "failed", "pending"])
                | Q(description_ai_source_hash="")
            )

        qs = qs.order_by("classId")
        if limit > 0:
            qs = qs[:limit]

        ids = list(qs.values_list("classId", flat=True))
        self.stdout.write(f"Enqueueing {len(ids)} description format task(s).")

        for i, pk in enumerate(ids):
            ClassesMain.objects.filter(pk=pk).update(description_ai_status="pending")
            format_class_description_task.delay(pk)
            if delay and i < len(ids) - 1:
                time.sleep(delay)

        self.stdout.write(self.style.SUCCESS("Done."))
