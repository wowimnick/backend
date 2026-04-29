"""
Enqueue or run Gemini description formatting for matching classes.

Without --sync: tasks are sent to Celery (`format_class_description_task.delay`).
**If no Celery worker is consuming the queue, nothing happens** until a worker runs.

With --sync: calls Gemini in this process (no broker/worker needed). Use for local/dev
or one-off production runs when workers are unavailable.

Requires GEMINI_API_KEY (and google-genai) for non-empty descriptions.
"""

import hashlib
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from quickstart.models import ClassesMain
from quickstart.tasks.business_tasks import format_class_description_task
from quickstart.utils.description_formatter import DescriptionFormatter


def _run_one_sync(pk: int, force: bool) -> str:
    """
    Mirror format_class_description_task logic for synchronous execution.
    Returns: ok | skipped_empty | skipped_hash | failed_api | error
    """
    try:
        instance = ClassesMain.objects.get(pk=pk)
    except ClassesMain.DoesNotExist:
        return "missing"

    raw = (instance.description or "").strip()
    if not raw:
        ClassesMain.objects.filter(pk=pk).update(
            description_summary="",
            description_sections=[],
            description_ai_source_hash="",
            description_ai_status="ready",
            description_ai_generated_at=timezone.now(),
        )
        return "skipped_empty"

    new_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if (
        not force
        and instance.description_ai_status == "ready"
        and instance.description_ai_source_hash == new_hash
    ):
        return "skipped_hash"

    DescriptionFormatter().process(instance, new_hash)
    instance.refresh_from_db()
    if instance.description_ai_status == "ready":
        return "ok"
    return "failed_api"


class Command(BaseCommand):
    help = (
        "Regenerate description_summary / description_sections via Gemini. "
        "Default: enqueue Celery tasks (--sync runs in this process instead)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max classes to process (0 = no limit).",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="Seconds to sleep between enqueue calls (async mode only).",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Include classes where status is not 'active' (default: active only).",
        )
        parser.add_argument(
            "--include-empty-description",
            action="store_true",
            help="Include rows with blank description (clears AI fields; no Gemini call).",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help=(
                "Run Gemini in this process (no Celery). Required if workers are not running."
            ),
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        delay = max(0.0, options["delay"])
        include_inactive = options["include_inactive"]
        include_empty = options["include_empty_description"]
        sync = options["sync"]

        if sync and not getattr(settings, "GEMINI_API_KEY", None):
            raise CommandError(
                "GEMINI_API_KEY is not set. Add it to your environment (e.g. .env) "
                "before using --sync. Async mode still queues tasks but workers need "
                "the key too when they run."
            )

        qs = ClassesMain.objects.all()
        if not include_inactive:
            qs = qs.filter(status="active")
        if not include_empty:
            qs = qs.exclude(description__exact="")

        qs = qs.order_by("classId")
        if limit > 0:
            qs = qs[:limit]

        ids = list(qs.values_list("classId", flat=True))
        if not ids:
            self.stdout.write(
                self.style.WARNING(
                    "No classes matched. Defaults: status=active and non-empty description. "
                    "Try --include-inactive and/or --include-empty-description."
                )
            )
            return

        if sync:
            self.stdout.write(
                self.style.WARNING(
                    f"Running Gemini synchronously for {len(ids)} class(es) (no Celery)."
                )
            )
            ok = skip = fail = 0
            for pk in ids:
                try:
                    result = _run_one_sync(pk, force=True)
                    if result == "ok":
                        ok += 1
                        self.stdout.write(f"  classId={pk} OK")
                    elif result in ("skipped_empty", "skipped_hash"):
                        skip += 1
                        self.stdout.write(f"  classId={pk} skipped ({result})")
                    else:
                        fail += 1
                        self.stdout.write(
                            self.style.WARNING(f"  classId={pk} -> {result}")
                        )
                except Exception as exc:
                    fail += 1
                    ClassesMain.objects.filter(pk=pk).update(
                        description_ai_status="failed"
                    )
                    self.stderr.write(f"  classId={pk} ERROR: {exc}")
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. ok={ok} skipped={skip} failed={fail}. "
                    "Check description_ai_status / logs if failed > 0."
                )
            )
            return

        # Async: Celery queue
        self.stdout.write(
            self.style.WARNING(
                "Queueing Celery tasks — if NO WORKER is running, these will sit in the "
                "broker until you start one. For immediate results without workers, "
                "re-run with --sync."
            )
        )
        self.stdout.write(f"Enqueueing {len(ids)} task(s) with force=True.")

        for i, pk in enumerate(ids):
            ClassesMain.objects.filter(pk=pk).update(description_ai_status="pending")
            format_class_description_task.delay(pk, force=True)
            if delay and i < len(ids) - 1:
                time.sleep(delay)

        self.stdout.write(self.style.SUCCESS("Tasks queued. Start/restart Celery workers to process them."))
