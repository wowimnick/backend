"""
Flush class search cache keys (preset, collection, category). Used on backend startup
and after invalidation to remove stale keys. Scoped by DJANGO_ENV so staging and prod
can share Redis without clearing each other's cache. Requires a cache backend that
supports delete_pattern (e.g. django_redis); no-op otherwise.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Prefixes used in cache key format: {prefix}:{env}:v{version}:...
# Must match public_class_views; keys are env-scoped so we only flush current env.
CLASS_SEARCH_CACHE_PREFIXES = (
    "public_class_search_preset",
    "public_class_search_collection",
    "public_class_search_preset_collection",
    "public_class_search_preset_category",
    "public_class_search_category_only",
)


def _get_cache_env():
    """Current environment for cache key scope (staging, prod, local)."""
    return getattr(settings, "DJANGO_ENV", "local")


def flush_all_class_search_cache(cache_backend):
    """
    Delete all class search cache keys for the current environment only. Call on
    backend startup so each deploy starts with a clean cache for its env without
    affecting staging/prod. No-op if backend does not support delete_pattern.
    """
    if not getattr(cache_backend, "delete_pattern", None):
        logger.debug("Cache backend has no delete_pattern; skipping class search cache flush.")
        return
    env = _get_cache_env()
    try:
        for prefix in CLASS_SEARCH_CACHE_PREFIXES:
            pattern = f"{prefix}:{env}:*"
            cache_backend.delete_pattern(pattern)
        logger.info(
            "Flushed class search cache keys for env=%s (%s prefixes).",
            env,
            len(CLASS_SEARCH_CACHE_PREFIXES),
        )
    except Exception as e:
        logger.warning("Failed to flush class search cache (env=%s): %s", env, e, exc_info=True)


def flush_class_search_cache_for_version(cache_backend, version):
    """
    Delete all class search cache keys for the current env and given version. Call
    after prewarm has filled the new version so stale keys are removed. No-op if
    backend does not support delete_pattern.
    """
    if not getattr(cache_backend, "delete_pattern", None):
        logger.debug("Cache backend has no delete_pattern; skipping version flush.")
        return
    env = _get_cache_env()
    try:
        for prefix in CLASS_SEARCH_CACHE_PREFIXES:
            pattern = f"{prefix}:{env}:v{version}:*"
            cache_backend.delete_pattern(pattern)
        logger.info(
            "Flushed class search cache keys for env=%s version %s (%s prefixes).",
            env,
            version,
            len(CLASS_SEARCH_CACHE_PREFIXES),
        )
    except Exception as e:
        logger.warning(
            "Failed to flush class search cache for env=%s version %s: %s",
            env,
            version,
            e,
            exc_info=True,
        )
