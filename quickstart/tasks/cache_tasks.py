"""
Celery tasks for cache prewarming. Prewarm runs only in production (IS_DEPLOYED_ENV).
"""
import logging

from celery import shared_task

from quickstart.utils.cache_prewarm import run_prewarm_class_search_cache

logger = logging.getLogger(__name__)


@shared_task(name="quickstart.tasks.cache_tasks.prewarm_class_search_cache")
def prewarm_class_search_cache_task(
    locations=True,
    collections=True,
    categories=True,
    version=None,
    location_names=None,
    collection_slugs=None,
    category_keys=None,
):
    """
    Prewarm class search cache for preset locations, collections, and location+category+subcategory.
    If version is set (e.g. after invalidation), prewarm fills cache for that version
    then bumps the live version and flushes stale keys for the previous version.
    Only runs when IS_DEPLOYED_ENV is True; otherwise no-op.

    Selective prewarm: pass location_names, collection_slugs, and/or category_keys (each a list or None for "all").
    """
    from django.conf import settings
    from django.core.cache import cache
    from quickstart.views.public.public_class_views import (
        PRESET_CACHE_VERSION_KEY,
    )
    from quickstart.utils.class_search_cache_flush import (
        flush_class_search_cache_for_version,
    )

    if not getattr(settings, "IS_DEPLOYED_ENV", False):
        logger.info("Skipping prewarm_class_search_cache_task (not production).")
        return

    run_prewarm_class_search_cache(
        locations=locations,
        collections=collections,
        categories=categories,
        cache_version=version,
        location_names=location_names,
        collection_slugs=collection_slugs,
        category_keys=category_keys,
    )

    # Only bump version and flush old when this was a full invalidation (version set).
    # Selective invalidation passes version=None: we only refilled deleted keys at current version.
    if version is not None:
        current = cache.get(PRESET_CACHE_VERSION_KEY, 0) or 0
        if version >= current:
            cache.set(PRESET_CACHE_VERSION_KEY, version, timeout=None)
            logger.info(
                "Prewarm complete; cache version set to %s.",
                version,
            )
            old_version = version - 1
            if old_version >= 0:
                flush_class_search_cache_for_version(cache, old_version)
