"""
Clear public caches (homepage + search preset version) so fresh data is served after deploy.
Run on web container startup so each deploy doesn't serve stale cached responses.
"""
from django.core.management.base import BaseCommand
from django.core.cache import cache


# Keys must match public_class_views to avoid circular import
HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY = "homepage_content_collections"
PRESET_CACHE_VERSION_KEY = "public_class_search_preset_version"


class Command(BaseCommand):
    help = "Clear homepage and search preset caches so next request gets fresh data (e.g. on deploy)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only log what would be done, do not change cache.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write("Dry run: no cache changes will be made.")

        try:
            # 1. Delete homepage content cache so next request rebuilds with current env (e.g. CLOUDFRONT_DOMAIN).
            self.stdout.write("Clearing homepage content cache...")
            if not dry_run:
                try:
                    cache.delete(HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY)
                    self.stdout.write(self.style.SUCCESS("  Homepage cache cleared."))
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING("  Redis unavailable (e.g. OOM): %s" % e)
                    )
            else:
                self.stdout.write(self.style.SUCCESS("  (dry run)"))

            # 2. Bump search preset version so old preset keys are no longer read; new requests use new keys.
            #    Old keys remain in Redis until TTL/eviction but are orphaned.
            self.stdout.write("Bumping search preset cache version...")
            if not dry_run:
                try:
                    version = cache.get(PRESET_CACHE_VERSION_KEY, 0) or 0
                    cache.set(PRESET_CACHE_VERSION_KEY, version + 1, timeout=None)
                    self.stdout.write(self.style.SUCCESS("  Search preset version bumped."))
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING("  Redis unavailable (e.g. OOM): %s" % e)
                    )
            else:
                self.stdout.write(self.style.SUCCESS("  (dry run)"))

            self.stdout.write(self.style.SUCCESS("Public caches cleared successfully."))
        except Exception as e:
            self.stdout.write(
                self.style.ERROR("Failed to clear public caches: %s" % e)
            )
            # Do not re-raise: allow web container to start even if Redis is down/OOM
            return
