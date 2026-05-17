"""
Celery tasks for syncing Instagram follower counts (Apify).

See instagram_scraper.fetch_follower_count and BusinessInfo instagram_* fields.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from quickstart.models import BusinessInfo
from quickstart.services.instagram_scraper import fetch_follower_count, normalize_instagram_handle

logger = logging.getLogger(__name__)

_STALE_AFTER = timedelta(days=6)


def schedule_instagram_sync_if_instagram_changed(
    business_id: int, old_instagram: str | None, new_instagram: str | None
) -> None:
    """Clear stored counts when URL removed; queue Apify scrape when URL set/changed."""
    old = (old_instagram or "").strip()
    new = (new_instagram or "").strip()
    if old == new:
        return
    if not new:
        BusinessInfo.objects.filter(pk=business_id).update(
            instagram_follower_count=None,
            instagram_sync_status="pending",
            instagram_followers_synced_at=None,
        )
        return
    sync_instagram_followers_for_business.delay(business_id)


def _instagram_url_from_business(business: BusinessInfo) -> str:
    links = business.social_media_links or {}
    return (links.get("instagram") or "").strip()


@shared_task
def sync_instagram_followers_for_business(business_id: int):
    try:
        business = BusinessInfo.objects.get(pk=business_id)
    except BusinessInfo.DoesNotExist:
        logger.warning("sync_instagram_followers_for_business: business %s not found", business_id)
        return

    url_or_handle = _instagram_url_from_business(business)
    if not url_or_handle or not normalize_instagram_handle(url_or_handle):
        BusinessInfo.objects.filter(pk=business_id).update(
            instagram_follower_count=None,
            instagram_followers_synced_at=timezone.now(),
            instagram_sync_status="pending",
        )
        return

    try:
        result = fetch_follower_count(url_or_handle)
    except Exception as e:
        logger.exception("Instagram sync failed for business %s: %s", business_id, e)
        BusinessInfo.objects.filter(pk=business_id).update(
            instagram_followers_synced_at=timezone.now(),
            instagram_sync_status="error",
        )
        return

    status = result.get("status") or "error"
    count = result.get("follower_count")
    now = timezone.now()

    row_updates = {
        "instagram_followers_synced_at": now,
        "instagram_sync_status": status if status != "skipped_no_token" else "error",
    }
    if status == "ok" and count is not None:
        row_updates["instagram_follower_count"] = int(count)
    elif status in ("not_found", "private", "error", "skipped_no_token"):
        row_updates["instagram_follower_count"] = None

    BusinessInfo.objects.filter(pk=business_id).update(**row_updates)


def _queue_instagram_for_business_ids(business_ids: list[int]) -> int:
    for idx, pk in enumerate(business_ids):
        sync_instagram_followers_for_business.apply_async(
            args=[pk],
            countdown=min(idx * 2, 300),
        )
    return len(business_ids)


@shared_task
def sync_instagram_followers_all():
    """Fan out per-business sync for businesses with Instagram links and stale sync (beat)."""
    cutoff = timezone.now() - _STALE_AFTER
    qs = (
        BusinessInfo.objects.filter(social_media_links__has_key="instagram")
        .exclude(social_media_links__instagram="")
        .exclude(social_media_links__instagram__isnull=True)
        .filter(
            Q(instagram_followers_synced_at__isnull=True)
            | Q(instagram_followers_synced_at__lt=cutoff)
        )
    )

    pks = list(qs.values_list("businessId", flat=True))
    n = _queue_instagram_for_business_ids(pks)
    logger.info(
        "sync_instagram_followers_all: queued %s businesses",
        n,
    )


@shared_task
def sync_instagram_followers_enqueue_all():
    """Fan out Instagram follower sync for every business with an Instagram URL (admin manual)."""
    pks = list(
        BusinessInfo.objects.filter(social_media_links__has_key="instagram")
        .exclude(social_media_links__instagram="")
        .exclude(social_media_links__instagram__isnull=True)
        .values_list("businessId", flat=True)
    )
    n = _queue_instagram_for_business_ids(pks)
    logger.info("sync_instagram_followers_enqueue_all: queued %s businesses", n)
