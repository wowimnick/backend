"""Queue the Typesense bootstrap Celery task (used by web entrypoint on deploy)."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Enqueue bootstrap_typesense_search_index_task when TYPESENSE_AUTO_BOOTSTRAP is enabled "
        "(no-op if Typesense is not configured)."
    )

    def handle(self, *args, **options):
        from quickstart.services.typesense_client import typesense_available
        from quickstart.tasks.search_index_tasks import enqueue_typesense_bootstrap_check

        if not getattr(settings, "TYPESENSE_AUTO_BOOTSTRAP", False):
            self.stdout.write(
                "TYPESENSE_AUTO_BOOTSTRAP is disabled; nothing scheduled."
            )
            return

        if not typesense_available():
            self.stdout.write(
                "Typesense is not configured (TYPESENSE_API_KEY); nothing scheduled."
            )
            return

        enqueue_typesense_bootstrap_check()
        delay = getattr(settings, "TYPESENSE_BOOTSTRAP_DELAY_SECONDS", 90)
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued Typesense bootstrap check (Celery countdown={delay}s)."
            )
        )
