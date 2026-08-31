"""
Clear public caches (homepage + class search) so fresh data is served after deploy.
Run on web container startup so each deploy doesn't serve stale cached responses.
Flushes class search cache keys for current env only (staging/prod share Redis safely),
then bumps version. Keys are env-scoped so staging and prod do not affect each other.

Use --once-per-build so only the first web container for a given build clears cache
(avoids clearing cache again when ECS scales out more web tasks with the same image).
Requires BUILD_ID, IMAGE_TAG, or GIT_SHA in the environment (set in ECS task definition at deploy).
"""
from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.conf import settings

from quickstart.utils.class_search_cache_flush import flush_all_class_search_cache
from quickstart.utils.deploy_build_id import get_deploy_build_id
from quickstart.views.public.public_class_views import (
    HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY,
    PRESET_CACHE_VERSION_KEY,
)

_CACHE_ENV = getattr(settings, "DJANGO_ENV", "local")
_CACHE_CLEARED_BUILD_KEY_PREFIX = "public_cache_cleared_build"
_CACHE_CLEARED_BUILD_TTL = 30 * 24 * 3600  # 30 days


class Command(BaseCommand):
    help = "Clear homepage and class search caches so next request gets fresh data (e.g. on deploy)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only log what would be done, do not change cache.",
        )
        parser.add_argument(
            "--once-per-build",
            action="store_true",
            help="Clear only if this build has not cleared yet (uses BUILD_ID/IMAGE_TAG/GIT_SHA). "
            "Use on web startup so scale-out does not clear cache again.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        once_per_build = options["once_per_build"]
        if dry_run:
            self.stdout.write("Dry run: no cache changes will be made.")

        if once_per_build:
            build_id = get_deploy_build_id()
            if not build_id:
                self.stdout.write(
                    self.style.WARNING(
                        "Once-per-build requested but no BUILD_ID/IMAGE_TAG/GIT_SHA set; skipping clear."
                    )
                )
                return
            cache_key = f"{_CACHE_CLEARED_BUILD_KEY_PREFIX}:{_CACHE_ENV}:{build_id}"
            try:
                if cache.get(cache_key):
                    self.stdout.write(
                        self.style.SUCCESS(
                            "Cache already cleared for this build (key=%s); skipping." % cache_key
                        )
                    )
                    return
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING("Could not check build marker in cache: %s" % e)
                )
                return

        self._do_clear(dry_run, once_per_build)

    def _do_clear(self, dry_run, once_per_build):
        successes = []  # track which operations succeeded (when not dry_run)
        try:
            # 1. Delete homepage content cache so next request rebuilds with current env (e.g. CLOUDFRONT_DOMAIN).
            self.stdout.write("Clearing homepage content cache...")
            if not dry_run:
                try:
                    cache.delete(HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY)
                    self.stdout.write(self.style.SUCCESS("  Homepage cache cleared."))
                    successes.append("homepage")
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING("  Redis unavailable (e.g. OOM): %s" % e)
                    )
            else:
                self.stdout.write(self.style.SUCCESS("  (dry run)"))

            # 2. Flush all class search cache keys (preset, collection, category) so no stale data remains.
            self.stdout.write("Flushing class search cache keys...")
            if not dry_run:
                try:
                    flush_all_class_search_cache(cache)
                    self.stdout.write(self.style.SUCCESS("  Class search cache flushed."))
                    successes.append("flush")
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING("  Flush failed (e.g. Redis OOM): %s" % e)
                    )
            else:
                self.stdout.write(self.style.SUCCESS("  (dry run)"))

            # 3. Bump search preset version so new keys use next version; prewarm/requests will repopulate.
            self.stdout.write("Bumping search preset cache version...")
            if not dry_run:
                try:
                    version = cache.get(PRESET_CACHE_VERSION_KEY, 0) or 0
                    cache.set(PRESET_CACHE_VERSION_KEY, version + 1, timeout=None)
                    self.stdout.write(self.style.SUCCESS("  Search preset version bumped."))
                    successes.append("version")
                except Exception as e:
                    self.stdout.write(
                        self.style.WARNING("  Redis unavailable (e.g. OOM): %s" % e)
                    )
            else:
                self.stdout.write(self.style.SUCCESS("  (dry run)"))

            # If once-per-build and all steps succeeded, mark this build as having cleared (so other tasks skip).
            if once_per_build and not dry_run and len(successes) == 3:
                build_id = get_deploy_build_id()
                if build_id:
                    cache_key = f"{_CACHE_CLEARED_BUILD_KEY_PREFIX}:{_CACHE_ENV}:{build_id}"
                    try:
                        cache.set(cache_key, "1", timeout=_CACHE_CLEARED_BUILD_TTL)
                        self.stdout.write(
                            self.style.SUCCESS("  Build marker set (key=%s)." % cache_key)
                        )
                    except Exception as e:
                        self.stdout.write(
                            self.style.WARNING("  Could not set build marker: %s" % e)
                        )

            # Report outcome: success only if all three operations succeeded (or dry run).
            if dry_run:
                self.stdout.write(self.style.SUCCESS("Public caches clear (dry run) completed."))
            elif len(successes) == 3:
                self.stdout.write(self.style.SUCCESS("Public caches cleared successfully."))
            elif successes:
                self.stdout.write(
                    self.style.WARNING(
                        "Public caches partially cleared (%s succeeded). Redis may be unavailable or OOM."
                        % ", ".join(successes)
                    )
                )
            else:
                self.stdout.write(
                    self.style.ERROR(
                        "Public caches not cleared: all operations failed. Redis may be unavailable or OOM."
                    )
                )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR("Failed to clear public caches: %s" % e)
            )
            # Do not re-raise: allow web container to start even if Redis is down/OOM
            return
