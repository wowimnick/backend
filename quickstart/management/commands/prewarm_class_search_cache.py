"""
Prewarm the class search cache for preset locations and collections so the first
user request is fast. In production, prewarm also runs automatically via Celery
after worker start and after cache invalidation.
"""
from django.core.management.base import BaseCommand

from quickstart.utils.cache_prewarm import run_prewarm_class_search_cache


class Command(BaseCommand):
    help = (
        "Prewarm class search cache for preset locations, collections, and location+category (24 and 50 results each). "
        "In production, this also runs automatically in the background via Celery."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--locations-only",
            action="store_true",
            help="Only prewarm preset location caches",
        )
        parser.add_argument(
            "--collections-only",
            action="store_true",
            help="Only prewarm collection caches",
        )
        parser.add_argument(
            "--categories-only",
            action="store_true",
            help="Only prewarm preset location + category caches",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even when not in production (IS_DEPLOYED_ENV=False)",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        if not options["force"] and not getattr(settings, "IS_DEPLOYED_ENV", False):
            self.stdout.write(
                self.style.WARNING(
                    "Skipping prewarm (not production). Use --force to run anyway."
                )
            )
            return

        only_categories = options["categories_only"]
        run_prewarm_class_search_cache(
            locations=not (options["collections_only"] or only_categories),
            collections=not (options["locations_only"] or only_categories),
            categories=not (options["locations_only"] or options["collections_only"]) or only_categories,
            skip_env_check=options["force"],
        )

        self.stdout.write(self.style.SUCCESS("Prewarm complete."))
