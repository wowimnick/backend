"""
Shared logic to prewarm class search cache (preset locations and collections).
Used by the management command and the Celery task. Only intended for production.
"""
import logging
import time

from django.test import Client

from quickstart.models import ClassCollection
from quickstart.services.search_geo_params import PRESET_LOCATIONS
from quickstart.views.public.public_class_views import PRESET_PREWARM_PAGE_SIZE

logger = logging.getLogger(__name__)

BASE_PATH = "/api/classes/search/"
PAGE_SIZES = [24, PRESET_PREWARM_PAGE_SIZE]
# Preset banner search uses infinite scroll; page=1 alone always cold-missed page 2+.
PREWARM_PRESET_LOCATION_PAGES = ["1", "2"]

# Delay between each search request to avoid hitting DRF anon throttle (e.g. 500/min).
# Overridable via settings.PREWARM_REQUEST_DELAY_SECONDS.
PREWARM_REQUEST_DELAY_SECONDS = 0.25


def run_prewarm_class_search_cache(
    locations=True,
    collections=True,
    skip_env_check=False,
    cache_version=None,
    location_names=None,
    collection_slugs=None,
):
    """
    Prewarm cache for preset locations and collections.
    Uses Django test client to hit the search API so responses are cached.
    If cache_version is set, cache keys use that version (so invalidation can prewarm
    new version before bumping live; users always get cached).
    No-op if not IS_DEPLOYED_ENV unless skip_env_check=True (e.g. for management command --force).

    Selective prewarm: location_names and/or collection_slugs (each a list or None for "all").
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
            location_names=location_names,
            collection_slugs=collection_slugs,
        )
    finally:
        if cache_version is not None:
            clear_prewarm_cache_version()


HOMEPAGE_CONTENT_PATH = "/api/classes/homepage-content/"


def _prewarm_delay():
    """Sleep between requests to stay under DRF anon rate limit (e.g. 500/min)."""
    from django.conf import settings
    delay = getattr(settings, "PREWARM_REQUEST_DELAY_SECONDS", PREWARM_REQUEST_DELAY_SECONDS)
    if delay and delay > 0:
        time.sleep(delay)


def _run_prewarm(
    locations=True,
    collections=True,
    location_names=None,
    collection_slugs=None,
):
    """
    Inner prewarm loop (no version wiring).
    location_names / collection_slugs: if set, only prewarm those; None means all.
    """
    client = Client()

    try:
        if collections:
            r_coll = client.get(HOMEPAGE_CONTENT_PATH, {"mode": "collections"})
            _prewarm_delay()
            if r_coll.status_code == 200:
                logger.info("Prewarm collections: OK (cached)")
    except Exception as e:
        logger.warning("Prewarm collections failed: %s", e, exc_info=True)

    locations_to_prewarm = (
        list(PRESET_LOCATIONS.items())
        if location_names is None
        else [(n, PRESET_LOCATIONS[n]) for n in location_names if n in PRESET_LOCATIONS]
    )
    if locations:
        for name, (lat, lng) in locations_to_prewarm:
            for page_size in PAGE_SIZES:
                for page in PREWARM_PRESET_LOCATION_PAGES:
                    params = {
                        "lat": lat,
                        "lng": lng,
                        "location_search": f"{name}, ON",
                        "page": page,
                        "page_size": str(page_size),
                    }
                    try:
                        resp = client.get(BASE_PATH, params)
                        _prewarm_delay()
                        if resp.status_code == 200:
                            count = len(resp.json().get("results", []))
                            logger.info(
                                "Prewarm preset %s (page=%s page_size=%s): %s results cached",
                                name,
                                page,
                                page_size,
                                count,
                            )
                        else:
                            logger.warning(
                                "Prewarm preset %s (page=%s page_size=%s): HTTP %s",
                                name,
                                page,
                                page_size,
                                resp.status_code,
                            )
                    except Exception as e:
                        logger.warning(
                            "Prewarm preset %s (page=%s page_size=%s) failed: %s",
                            name,
                            page,
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
                    _prewarm_delay()
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
                        _prewarm_delay()
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
