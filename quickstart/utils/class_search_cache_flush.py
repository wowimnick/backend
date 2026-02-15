"""
Flush class search cache keys (preset, collection, category). Used on backend startup,
after full invalidation (version bump), and for selective invalidation (content update).
Scoped by DJANGO_ENV so staging and prod can share Redis without clearing each other's cache.
Requires a cache backend that supports delete_pattern (e.g. django_redis); no-op otherwise.
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

# Key segment order (after prefix:env:v*): used for selective flush patterns.
# PRESET: location_slug, ...
# COLLECTION: slug, ...
# PRESET_COLLECTION: location_slug, coll_slug, ...
# PRESET_CATEGORY: location_slug, cat_slug, sub_slug, ...
# CATEGORY_ONLY: cat_slug, sub_slug, ...


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


def _slug(s):
    """Normalize for cache key segment (match public_class_views)."""
    return (s or "").lower().replace(" ", "_")


def flush_class_search_cache_for_affected(
    cache_backend,
    affected_category_keys=None,
    affected_collection_slugs=None,
    affected_location_names=None,
):
    """
    Delete only cache keys that involve the given categories, collections, or preset
    locations. Used on content update (class/category/collection change) so unaffected
    keys stay valid. No version bump. No-op if backend does not support delete_pattern.
    """
    if not getattr(cache_backend, "delete_pattern", None):
        logger.debug("Cache backend has no delete_pattern; skipping selective flush.")
        return
    env = _get_cache_env()
    try:
        # Category-only keys: ...:v*:cat_slug:sub_slug:...
        if affected_category_keys:
            for key in affected_category_keys:
                cat_slug = _slug(key)
                if not cat_slug:
                    continue
                cache_backend.delete_pattern(
                    f"public_class_search_category_only:{env}:v*:{cat_slug}:*"
                )
                cache_backend.delete_pattern(
                    f"public_class_search_preset_category:{env}:v*:*:{cat_slug}:*"
                )
        # Collection-only keys: ...:v*:slug:... or ...:v*:location_slug:coll_slug:...
        if affected_collection_slugs:
            for slug in affected_collection_slugs:
                coll_slug = _slug(slug)
                if not coll_slug:
                    continue
                cache_backend.delete_pattern(
                    f"public_class_search_collection:{env}:v*:{coll_slug}:*"
                )
                cache_backend.delete_pattern(
                    f"public_class_search_preset_collection:{env}:v*:*:{coll_slug}:*"
                )
        # Preset location keys: ...:v*:location_slug:...
        if affected_location_names:
            for name in affected_location_names:
                loc_slug = _slug(name)
                if not loc_slug:
                    continue
                cache_backend.delete_pattern(
                    f"public_class_search_preset:{env}:v*:{loc_slug}:*"
                )
                cache_backend.delete_pattern(
                    f"public_class_search_preset_collection:{env}:v*:{loc_slug}:*"
                )
                cache_backend.delete_pattern(
                    f"public_class_search_preset_category:{env}:v*:{loc_slug}:*"
                )
        if affected_category_keys or affected_collection_slugs or affected_location_names:
            logger.info(
                "Flushed affected class search cache keys for env=%s (categories=%s, collections=%s, locations=%s).",
                env,
                len(affected_category_keys or ()),
                len(affected_collection_slugs or ()),
                len(affected_location_names or ()),
            )
    except Exception as e:
        logger.warning(
            "Failed to flush affected class search cache for env=%s: %s",
            env,
            e,
            exc_info=True,
        )
