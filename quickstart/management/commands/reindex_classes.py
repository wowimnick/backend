"""Full rebuild of Typesense class search collection and alias swap."""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Rebuild Typesense class search index and point alias classes_live to a new "
        "physical collection. Run after schema changes (e.g. booking_types). "
        "Requires TYPESENSE_* configuration."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=200,
            help="Documents per import batch",
        )

    def handle(self, *args, **options):
        from quickstart.services.search_index_service import run_full_typesense_reindex
        from quickstart.services.typesense_client import typesense_available

        if not typesense_available():
            self.stderr.write(
                "Typesense is not configured (TYPESENSE_API_KEY). Nothing to do."
            )
            return

        batch = max(10, int(options["batch_size"]))
        stats = run_full_typesense_reindex(batch_size=batch)
        self.stdout.write(
            self.style.SUCCESS(
                f'Done. alias={stats["alias"]} physical={stats["physical"]} '
                f'indexed={stats["indexed"]} scanned={stats["scanned"]}'
            )
        )
