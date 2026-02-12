"""
Celery tasks for cache prewarming. Prewarm runs only in production (IS_DEPLOYED_ENV).
"""
import logging

from celery import shared_task

from quickstart.utils.cache_prewarm import run_prewarm_class_search_cache

logger = logging.getLogger(__name__)


@shared_task(name="quickstart.tasks.cache_tasks.prewarm_class_search_cache")
def prewarm_class_search_cache_task(
    locations=True, collections=True, categories=True, version=None
):
    """
    Prewarm class search cache for preset locations, collections, and location+category.
    If version is set (e.g. after invalidation), prewarm fills cache for that version
    then bumps the live version so users always get cached responses.
    Only runs when IS_DEPLOYED_ENV is True; otherwise no-op.
    """
    from django.conf import settings
    from django.core.cache import cache
    from quickstart.views.public.public_class_views import (
        PRESET_CACHE_VERSION_KEY,
    )

    if not getattr(settings, "IS_DEPLOYED_ENV", False):
        logger.info("Skipping prewarm_class_search_cache_task (not production).")
        return

    run_prewarm_class_search_cache(
        locations=locations,
        collections=collections,
        categories=categories,
        cache_version=version,
    )

    if version is not None:
        current = cache.get(PRESET_CACHE_VERSION_KEY, 0) or 0
        if version >= current:
            cache.set(PRESET_CACHE_VERSION_KEY, version, timeout=None)
            logger.info(
                "Prewarm complete; cache version set to %s.",
                version,
            )
