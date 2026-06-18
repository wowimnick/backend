"""
Refresh the Gemini-selected homepage featured reviews.

Runs once per build on the web container (called from entrypoint.sh) so each
deploy atomically swaps the displayed reviews on the homepage hero. Gemini
picks the most compelling, diverse reviews from the imported Google reviews
pool; each is linked to a representative class slug for deep-linking.

Use --once-per-build so only the first web container for a given build runs
the Gemini call (avoids duplicate work when ECS scales out more web tasks with
the same image). Requires BUILD_ID, IMAGE_TAG, or GIT_SHA in the environment.

Can also be run manually without --once-per-build to force a refresh.
"""
from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.conf import settings

from quickstart.utils.deploy_build_id import get_deploy_build_id
from quickstart.utils.review_ai_service import (
    select_featured_homepage_reviews,
    DEFAULT_FEATURED_COUNT,
)

_CACHE_ENV = getattr(settings, "DJANGO_ENV", "local")
_REFRESHED_BUILD_KEY_PREFIX = "homepage_reviews_refreshed_build"
_REFRESHED_BUILD_TTL = 30 * 24 * 3600  # 30 days


class Command(BaseCommand):
    help = "Refresh Gemini-selected homepage featured reviews (once per build)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once-per-build",
            action="store_true",
            help="Refresh only if this build has not refreshed yet (uses BUILD_ID/IMAGE_TAG/GIT_SHA).",
        )
        parser.add_argument(
            "--count",
            type=int,
            default=DEFAULT_FEATURED_COUNT,
            help="Number of reviews Gemini should select (default: %(default)s).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only log what would be done, do not call Gemini or write rows.",
        )

    def handle(self, *args, **options):
        once_per_build = options["once_per_build"]
        count = options["count"]
        dry_run = options["dry_run"]

        if once_per_build:
            build_id = get_deploy_build_id()
            if not build_id:
                self.stdout.write(self.style.WARNING(
                    "Once-per-build requested but no BUILD_ID/IMAGE_TAG/GIT_SHA set; skipping."
                ))
                return
            cache_key = f"{_REFRESHED_BUILD_KEY_PREFIX}:{_CACHE_ENV}:{build_id}"
            try:
                if cache.get(cache_key):
                    self.stdout.write(self.style.SUCCESS(
                        f"Homepage reviews already refreshed for this build ({cache_key}); skipping."
                    ))
                    return
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"Could not check build marker in cache: {e}"
                ))
                return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry run: would call Gemini to select {count} featured reviews."
            ))
            return

        self.stdout.write(f"Selecting {count} featured homepage reviews via Gemini...")
        persisted = select_featured_homepage_reviews(count=count)

        if persisted > 0:
            self.stdout.write(self.style.SUCCESS(
                f"Refreshed homepage featured reviews: {persisted} rows persisted."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "No featured reviews persisted (Gemini failure or empty pool). "
                "Endpoint will fall back to a random sample."
            ))
            return

        # Mark this build as done so other web containers skip.
        if once_per_build:
            build_id = get_deploy_build_id()
            if build_id:
                cache_key = f"{_REFRESHED_BUILD_KEY_PREFIX}:{_CACHE_ENV}:{build_id}"
                try:
                    cache.set(cache_key, "1", timeout=_REFRESHED_BUILD_TTL)
                    self.stdout.write(self.style.SUCCESS(f"  Build marker set ({cache_key})."))
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"  Could not set build marker: {e}"))
