"""Cache-bust helpers for class/business detail API responses.

Copied out of deprecated marketplace public views so live business/admin
saves can still invalidate Next.js-facing API caches.
"""

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

CLASS_DETAIL_CACHE_VERSION_PREFIX = "class_detail_version"
BUSINESS_DETAIL_CACHE_VERSION_PREFIX = "business_detail_version"


def invalidate_class_detail_cache(slug=None, class_id=None):
    """Invalidate cached class detail API responses (slug and/or numeric id lookup)."""
    keys = []
    if slug:
        keys.append(f"{CLASS_DETAIL_CACHE_VERSION_PREFIX}:slug:{slug}")
    if class_id is not None:
        keys.append(f"{CLASS_DETAIL_CACHE_VERSION_PREFIX}:id:{class_id}")
    for version_key in keys:
        try:
            version = cache.get(version_key, 0) or 0
            cache.set(version_key, version + 1, timeout=None)
            logger.info(
                "Invalidated class detail cache for %s (version -> %s)",
                version_key,
                version + 1,
            )
        except Exception as e:
            logger.warning(
                "Failed to invalidate class detail cache for %s: %s",
                version_key,
                e,
            )


def invalidate_business_detail_cache(slug):
    """Invalidate cached business detail API response for this slug."""
    if not slug:
        return
    try:
        version = cache.get(f"{BUSINESS_DETAIL_CACHE_VERSION_PREFIX}:{slug}", 0) or 0
        cache.set(
            f"{BUSINESS_DETAIL_CACHE_VERSION_PREFIX}:{slug}", version + 1, timeout=None
        )
        logger.info(
            "Invalidated business detail cache for slug=%s (version -> %s)",
            slug,
            version + 1,
        )
    except Exception as e:
        logger.warning(
            "Failed to invalidate business detail cache for slug=%s: %s", slug, e
        )
