"""
Shared logic to prewarm class search cache (preset locations, collections, categories).
Used by the management command and the Celery task. Only intended for production.
"""
import logging

from django.test import Client

from quickstart.models import ClassCategory, ClassCollection
from quickstart.views.public.public_class_views import (
    PRESET_LOCATIONS,
    PRESET_PREWARM_PAGE_SIZE,
)

logger = logging.getLogger(__name__)

BASE_PATH = "/api/classes/search/"
PAGE_SIZES = [24, PRESET_PREWARM_PAGE_SIZE]


def run_prewarm_class_search_cache(
    locations=True,
    collections=True,
    categories=True,
    skip_env_check=False,
    cache_version=None,
):
    """
    Prewarm cache for preset locations, collections, and/or location+category combos.
    Uses Django test client to hit the search API so responses are cached.
    If cache_version is set, cache keys use that version (so invalidation can prewarm
    new version before bumping live; users always get cached).
    No-op if not IS_DEPLOYED_ENV unless skip_env_check=True (e.g. for management command --force).
    """
    from django.conf import settings
    from quickstart.views.public.public_class_views import (
        set_prewarm_cache_version,
        clear_prewarm_cache_version,
    )

    if not skip_env_check and not getattr(settings, "IS_DEPLOYED_ENV", False):
        logger.info("Skipping class search cache prewarm (not production).")
        return

    if cache_version is not None:
        set_prewarm_cache_version(cache_version)
    try:
        _run_prewarm(
            locations=locations,
            collections=collections,
            categories=categories,
        )
    finally:
        if cache_version is not None:
            clear_prewarm_cache_version()


def _run_prewarm(locations=True, collections=True, categories=True):
    """Inner prewarm loop (no version wiring)."""
    client = Client()
    if locations:
        for name, (lat, lng) in PRESET_LOCATIONS.items():
            for page_size in PAGE_SIZES:
                params = {
                    "lat": lat,
                    "lng": lng,
                    "location": name,
                    "page": "1",
                    "page_size": str(page_size),
                }
                try:
                    resp = client.get(BASE_PATH, params)
                    if resp.status_code == 200:
                        count = len(resp.json().get("results", []))
                        logger.info(
                            "Prewarm preset %s (page_size=%s): %s results cached",
                            name,
                            page_size,
                            count,
                        )
                    else:
                        logger.warning(
                            "Prewarm preset %s (page_size=%s): HTTP %s",
                            name,
                            page_size,
                            resp.status_code,
                        )
                except Exception as e:
                    logger.warning(
                        "Prewarm preset %s (page_size=%s) failed: %s",
                        name,
                        page_size,
                        e,
                        exc_info=True,
                    )

    if collections:
        for coll in ClassCollection.objects.filter(is_active=True).order_by(
            "sort_order"
        ):
            slug = coll.slug
            if not slug:
                continue
            for page_size in PAGE_SIZES:
                params = {
                    "collection": slug,
                    "page": "1",
                    "page_size": str(page_size),
                }
                try:
                    resp = client.get(BASE_PATH, params)
                    if resp.status_code == 200:
                        count = len(resp.json().get("results", []))
                        logger.info(
                            "Prewarm collection %s (page_size=%s): %s results cached",
                            coll.name or slug,
                            page_size,
                            count,
                        )
                    else:
                        logger.warning(
                            "Prewarm collection %s (page_size=%s): HTTP %s",
                            coll.name or slug,
                            page_size,
                            resp.status_code,
                        )
                except Exception as e:
                    logger.warning(
                        "Prewarm collection %s (page_size=%s) failed: %s",
                        coll.name or slug,
                        page_size,
                        e,
                            exc_info=True,
                        )

    # Category-only and category+subcategory (no location) — explore page instant load
    if categories:
        for cat in ClassCategory.objects.exclude(key="").exclude(key__isnull=True).order_by(
            "name"
        ):
            ckey = (cat.key or "").strip()
            if not ckey:
                continue
            for page_size in PAGE_SIZES:
                params = {
                    "category_key": ckey,
                    "page": "1",
                    "page_size": str(page_size),
                }
                try:
                    resp = client.get(BASE_PATH, params)
                    if resp.status_code == 200:
                        count = len(resp.json().get("results", []))
                        logger.info(
                            "Prewarm category %s (page_size=%s): %s results cached",
                            ckey,
                            page_size,
                            count,
                        )
                    else:
                        logger.warning(
                            "Prewarm category %s (page_size=%s): HTTP %s",
                            ckey,
                            page_size,
                            resp.status_code,
                        )
                except Exception as e:
                    logger.warning(
                        "Prewarm category %s (page_size=%s) failed: %s",
                        ckey,
                        page_size,
                        e,
                        exc_info=True,
                    )
            # Category + each subcategory
            for sub in cat.subcategories.all().order_by("name"):
                skey = (getattr(sub, "key", None) or "").strip()
                if not skey:
                    continue
                for page_size in PAGE_SIZES:
                    params = {
                        "category_key": ckey,
                        "subcategory_key": skey,
                        "page": "1",
                        "page_size": str(page_size),
                    }
                    try:
                        resp = client.get(BASE_PATH, params)
                        if resp.status_code == 200:
                            count = len(resp.json().get("results", []))
                            logger.info(
                                "Prewarm category %s + subcategory %s (page_size=%s): %s results cached",
                                ckey,
                                skey,
                                page_size,
                                count,
                            )
                        else:
                            logger.warning(
                                "Prewarm category %s + subcategory %s (page_size=%s): HTTP %s",
                                ckey,
                                skey,
                                page_size,
                                resp.status_code,
                            )
                    except Exception as e:
                        logger.warning(
                            "Prewarm category %s + subcategory %s (page_size=%s) failed: %s",
                            ckey,
                            skey,
                            page_size,
                            e,
                            exc_info=True,
                        )

    # Preset location + category (e.g. Toronto + Arts) — explore page category filters with location
    if locations and categories:
        for name, (lat, lng) in PRESET_LOCATIONS.items():
            for cat in ClassCategory.objects.exclude(key="").exclude(key__isnull=True).order_by(
                "name"
            ):
                ckey = (cat.key or "").strip()
                if not ckey:
                    continue
                for page_size in PAGE_SIZES:
                    params = {
                        "lat": lat,
                        "lng": lng,
                        "location": name,
                        "location_search": f"{name}, ON",
                        "category_key": ckey,
                        "page": "1",
                        "page_size": str(page_size),
                    }
                    try:
                        resp = client.get(BASE_PATH, params)
                        if resp.status_code == 200:
                            count = len(resp.json().get("results", []))
                            logger.info(
                                "Prewarm %s + category %s (page_size=%s): %s results cached",
                                name,
                                ckey,
                                page_size,
                                count,
                            )
                        else:
                            logger.warning(
                                "Prewarm %s + category %s (page_size=%s): HTTP %s",
                                name,
                                ckey,
                                page_size,
                                resp.status_code,
                            )
                    except Exception as e:
                        logger.warning(
                            "Prewarm %s + category %s (page_size=%s) failed: %s",
                            name,
                            ckey,
                            page_size,
                            e,
                            exc_info=True,
                        )

    # Preset location + collection (e.g. Toronto + trending) — what the frontend often sends
    if locations and collections:
        for name, (lat, lng) in PRESET_LOCATIONS.items():
            for coll in ClassCollection.objects.filter(is_active=True).order_by(
                "sort_order"
            ):
                slug = coll.slug
                if not slug:
                    continue
                for page_size in PAGE_SIZES:
                    params = {
                        "lat": lat,
                        "lng": lng,
                        "location": name,
                        "location_search": f"{name}, ON",
                        "collection": slug,
                        "page": "1",
                        "page_size": str(page_size),
                    }
                    try:
                        resp = client.get(BASE_PATH, params)
                        if resp.status_code == 200:
                            count = len(resp.json().get("results", []))
                            logger.info(
                                "Prewarm %s + %s (page_size=%s): %s results cached",
                                name,
                                coll.name or slug,
                                page_size,
                                count,
                            )
                        else:
                            logger.warning(
                                "Prewarm %s + %s (page_size=%s): HTTP %s",
                                name,
                                coll.name or slug,
                                page_size,
                                resp.status_code,
                            )
                    except Exception as e:
                        logger.warning(
                            "Prewarm %s + %s (page_size=%s) failed: %s",
                            name,
                            coll.name or slug,
                            page_size,
                            e,
                            exc_info=True,
                        )

    logger.info("Class search cache prewarm finished.")
