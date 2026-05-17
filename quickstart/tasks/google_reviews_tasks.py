"""
Celery tasks for syncing Google Maps reviews (Apify) into ImportedGoogleReview.
"""

import logging
from datetime import timedelta

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from django.db.models import Q
from django.utils import timezone

from quickstart.models import BusinessInfo
from quickstart.services.google_reviews_importer import import_reviews_for_business
from quickstart.services.google_reviews_scraper import fetch_reviews

logger = logging.getLogger(__name__)

_STALE_AFTER = timedelta(days=12)


def schedule_google_reviews_sync_if_url_changed(
    business_id: int, old_url: str | None, new_url: str | None
) -> None:
    """Clear sync metadata when URL removed; queue scrape when URL set/changed."""
    old = (old_url or "").strip()
    new = (new_url or "").strip()
    if old == new:
        return
    if not new:
        BusinessInfo.objects.filter(pk=business_id).update(
            google_reviews_synced_at=None,
            google_reviews_sync_status="pending",
            google_reviews_last_scraped_count=0,
        )
        return
    sync_google_reviews_for_business.delay(business_id)


@shared_task(bind=True, max_retries=2)
def sync_google_reviews_for_business(self, business_id: int):
    try:
        business = BusinessInfo.objects.get(pk=business_id)
    except BusinessInfo.DoesNotExist:
        logger.warning("sync_google_reviews_for_business: business %s not found", business_id)
        return

    url = (business.google_maps_url or "").strip()
    if not url:
        BusinessInfo.objects.filter(pk=business_id).update(
            google_reviews_synced_at=timezone.now(),
            google_reviews_sync_status="pending",
        )
        return

    BusinessInfo.objects.filter(pk=business_id).update(
        google_reviews_sync_status="running",
    )

    try:
        result = fetch_reviews(url)
    except Exception as e:
        logger.exception("Google reviews fetch failed for business %s: %s", business_id, e)
        try:
            raise self.retry(exc=e, countdown=300)
        except MaxRetriesExceededError:
            BusinessInfo.objects.filter(pk=business_id).update(
                google_reviews_synced_at=timezone.now(),
                google_reviews_sync_status="error",
            )
            return

    scrape_status = result.get("status") or "error"
    if scrape_status == "skipped_no_token":
        logger.warning("APIFY_TOKEN missing; Google reviews sync skipped for %s", business_id)
        BusinessInfo.objects.filter(pk=business_id).update(
            google_reviews_synced_at=timezone.now(),
            google_reviews_sync_status="error",
        )
        return

    if scrape_status != "ok":
        BusinessInfo.objects.filter(pk=business_id).update(
            google_reviews_synced_at=timezone.now(),
            google_reviews_sync_status=scrape_status,
            google_reviews_last_scraped_count=0,
        )
        return

    reviews = result.get("reviews") or []
    try:
        import_reviews_for_business(
            business,
            reviews,
            skip_images=False,
            full_refresh=True,
        )
    except Exception as e:
        logger.exception("Google reviews import failed for business %s: %s", business_id, e)
        try:
            raise self.retry(exc=e, countdown=300)
        except MaxRetriesExceededError:
            BusinessInfo.objects.filter(pk=business_id).update(
                google_reviews_synced_at=timezone.now(),
                google_reviews_sync_status="error",
            )
            return

    BusinessInfo.objects.filter(pk=business_id).update(
        google_reviews_synced_at=timezone.now(),
        google_reviews_sync_status="ok",
        google_reviews_last_scraped_count=len(reviews),
    )


def _queue_google_reviews_for_business_ids(business_ids: list[int]) -> int:
    """Stagger Apify-heavy syncs."""
    for idx, pk in enumerate(business_ids):
        sync_google_reviews_for_business.apply_async(
            args=[pk],
            countdown=min(idx * 30, 1800),
        )
    return len(business_ids)


@shared_task
def sync_google_reviews_all():
    """Fan out per-business sync for businesses with a Maps URL and stale sync (beat)."""
    cutoff = timezone.now() - _STALE_AFTER
    qs = (
        BusinessInfo.objects.exclude(google_maps_url__isnull=True)
        .exclude(google_maps_url="")
        .filter(
            Q(google_reviews_synced_at__isnull=True)
            | Q(google_reviews_synced_at__lt=cutoff)
        )
    )

    pks = list(qs.values_list("businessId", flat=True))
    n = _queue_google_reviews_for_business_ids(pks)
    logger.info("sync_google_reviews_all: queued %s businesses", n)


@shared_task
def sync_google_reviews_enqueue_all():
    """Fan out Google reviews sync for every business with a Maps URL (admin manual)."""
    pks = list(
        BusinessInfo.objects.exclude(google_maps_url__isnull=True)
        .exclude(google_maps_url="")
        .values_list("businessId", flat=True)
    )
    n = _queue_google_reviews_for_business_ids(pks)
    logger.info("sync_google_reviews_enqueue_all: queued %s businesses", n)
