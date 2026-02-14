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
    location_names=None,
    collection_slugs=None,
    category_keys=None,
):
    """
    Prewarm cache for preset locations, collections, and/or location+category combos.
    Uses Django test client to hit the search API so responses are cached.
    If cache_version is set, cache keys use that version (so invalidation can prewarm
    new version before bumping live; users always get cached).
    No-op if not IS_DEPLOYED_ENV unless skip_env_check=True (e.g. for management command --force).

    Selective prewarm (only repopulate what changed):
    - location_names: if provided, only these preset location names (e.g. ["Toronto", "Ottawa"]).
      None = prewarm all preset locations when locations=True.
    - collection_slugs: if provided, only these collection slugs. None = prewarm all when collections=True.
    - category_keys: if provided, only these category keys. None = prewarm all when categories=True.
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
            location_names=location_names,
            collection_slugs=collection_slugs,
            category_keys=category_keys,
        )
    finally:
        if cache_version is not None:
            clear_prewarm_cache_version()


HOMEPAGE_CONTENT_PATH = "/api/classes/homepage-content/"
CATEGORIES_PATH = "/api/categories/"


def _run_prewarm(
    locations=True,
    collections=True,
    categories=True,
    location_names=None,
    collection_slugs=None,
    category_keys=None,
):
    """
    Inner prewarm loop (no version wiring).
    location_names / collection_slugs / category_keys: if set, only prewarm those;
    None means all (when locations/collections/categories is True).
    """
    client = Client()

    # Prewarm categories list and collections list when we're touching those dimensions
    need_categories_list = categories
    need_collections_list = collections
    try:
        if need_categories_list:
            r_cat = client.get(CATEGORIES_PATH)
            if r_cat.status_code == 200:
                logger.info("Prewarm categories: OK (cached)")
        if need_collections_list:
            r_coll = client.get(HOMEPAGE_CONTENT_PATH, {"mode": "collections"})
            if r_coll.status_code == 200:
                logger.info("Prewarm collections: OK (cached)")
    except Exception as e:
        logger.warning("Prewarm categories/collections failed: %s", e, exc_info=True)

    locations_to_prewarm = (
        list(PRESET_LOCATIONS.items())
        if location_names is None
        else [(n, PRESET_LOCATIONS[n]) for n in location_names if n in PRESET_LOCATIONS]
    )
    if locations:
        for name, (lat, lng) in locations_to_prewarm:
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

    collections_qs = ClassCollection.objects.filter(is_active=True).order_by(
        "sort_order"
    )
    if collection_slugs is not None:
        collections_qs = collections_qs.filter(slug__in=collection_slugs)
    if collections:
        for coll in collections_qs:
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
    categories_qs = ClassCategory.objects.exclude(key="").exclude(key__isnull=True).order_by(
        "name"
    )
    if category_keys is not None:
        categories_qs = categories_qs.filter(key__in=category_keys)
    if categories:
        for cat in categories_qs:
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
        for name, (lat, lng) in locations_to_prewarm:
            for cat in categories_qs:
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
                # Preset location + category + each subcategory (all combinations cached)
                for sub in cat.subcategories.all().order_by("name"):
                    skey = (getattr(sub, "key", None) or "").strip()
                    if not skey:
                        continue
                    for page_size in PAGE_SIZES:
                        params = {
                            "lat": lat,
                            "lng": lng,
                            "location": name,
                            "location_search": f"{name}, ON",
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
                                    "Prewarm %s + category %s + subcategory %s (page_size=%s): %s results cached",
                                    name,
                                    ckey,
                                    skey,
                                    page_size,
                                    count,
                                )
                            else:
                                logger.warning(
                                    "Prewarm %s + %s + %s (page_size=%s): HTTP %s",
                                    name,
                                    ckey,
                                    skey,
                                    page_size,
                                    resp.status_code,
                                )
                        except Exception as e:
                            logger.warning(
                                "Prewarm %s + %s + %s (page_size=%s) failed: %s",
                                name,
                                ckey,
                                skey,
                                page_size,
                                e,
                                exc_info=True,
                            )

    # Preset location + collection (e.g. Toronto + trending) — what the frontend often sends
    if locations and collections:
        for name, (lat, lng) in locations_to_prewarm:
            for coll in collections_qs:
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
