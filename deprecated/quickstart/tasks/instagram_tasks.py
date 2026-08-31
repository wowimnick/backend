"""
Celery tasks for syncing Instagram follower counts (Apify).

See instagram_scraper.fetch_follower_count and BusinessInfo instagram_* fields.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import DatabaseError, OperationalError
from django.db.models import Q
from django.utils import timezone

from quickstart.models import BusinessInfo
from quickstart.services.instagram_scraper import fetch_follower_count, normalize_instagram_handle

logger = logging.getLogger(__name__)

_STALE_AFTER = timedelta(days=6)


def _update_business_instagram_fields(business_id: int, fields: dict) -> int:
    """Persist Instagram sync columns; never raises — logs DB errors."""
    try:
        return BusinessInfo.objects.filter(pk=business_id).update(**fields)
    except (DatabaseError, OperationalError, TypeError, ValueError):
        logger.exception(
            "Instagram sync: DB update failed business_id=%s fields=%s",
            business_id,
            list(fields.keys()),
        )
        return -1
    except Exception:
        logger.exception(
            "Instagram sync: unexpected DB error business_id=%s",
            business_id,
        )
        return -1


def schedule_instagram_sync_if_instagram_changed(
    business_id: int, old_instagram: str | None, new_instagram: str | None
) -> None:
    """Clear stored counts when URL removed; queue Apify scrape when URL set/changed."""
    try:
        bid = int(business_id)
    except (TypeError, ValueError):
        logger.warning(
            "schedule_instagram_sync_if_instagram_changed: invalid business_id=%r",
            business_id,
        )
        return
    try:
        old = (old_instagram or "").strip()
        new = (new_instagram or "").strip()
        if old == new:
            return
        if not new:
            _update_business_instagram_fields(
                bid,
                {
                    "instagram_follower_count": None,
                    "instagram_sync_status": "pending",
                    "instagram_followers_synced_at": None,
                },
            )
            return
        sync_instagram_followers_for_business.delay(bid)
    except Exception:
        logger.exception(
            "schedule_instagram_sync_if_instagram_changed failed business_id=%s",
            bid,
        )


def _instagram_url_from_business(business: BusinessInfo) -> str:
    try:
        links = business.social_media_links
        if not isinstance(links, dict):
            return ""
        raw = links.get("instagram")
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw.strip()
        return str(raw).strip()
    except Exception:
        logger.warning(
            "Could not read social_media_links.instagram for business_id=%s",
            getattr(business, "pk", None),
            exc_info=True,
        )
        return ""


def _sync_instagram_followers_core(business_id: int) -> dict:
    token_ok = bool((getattr(settings, "APIFY_TOKEN", None) or "").strip())
    logger.info(
        "sync_instagram_followers_for_business start business_id=%s apify_token_configured=%s",
        business_id,
        token_ok,
    )
    try:
        business = BusinessInfo.objects.get(pk=business_id)
    except BusinessInfo.DoesNotExist:
        logger.warning(
            "sync_instagram_followers_for_business: business %s not found",
            business_id,
        )
        return {"business_id": business_id, "ok": False, "reason": "business_not_found"}
    except (DatabaseError, OperationalError):
        logger.exception(
            "sync_instagram_followers_for_business: DB error loading business %s",
            business_id,
        )
        return {"business_id": business_id, "ok": False, "reason": "db_error_load"}

    url_or_handle = _instagram_url_from_business(business)
    try:
        normalized = normalize_instagram_handle(url_or_handle) if url_or_handle else None
    except Exception:
        logger.exception(
            "normalize_instagram_handle failed business_id=%s",
            business_id,
        )
        normalized = None

    if not url_or_handle or not normalized:
        logger.info(
            "sync_instagram_followers_for_business skip business_id=%s slug=%s reason=no_valid_instagram "
            "raw_len=%s",
            business_id,
            getattr(business, "slug", None),
            len(url_or_handle or ""),
        )
        _update_business_instagram_fields(
            business_id,
            {
                "instagram_follower_count": None,
                "instagram_followers_synced_at": timezone.now(),
                "instagram_sync_status": "pending",
            },
        )
        return {
            "business_id": business_id,
            "ok": True,
            "reason": "no_valid_instagram",
            "instagram_sync_status": "pending",
        }

    logger.info(
        "sync_instagram_followers_for_business fetching business_id=%s handle=%s",
        business_id,
        normalized,
    )
    try:
        result = fetch_follower_count(url_or_handle)
    except Exception as e:
        logger.exception("Instagram sync failed for business %s: %s", business_id, e)
        _update_business_instagram_fields(
            business_id,
            {
                "instagram_followers_synced_at": timezone.now(),
                "instagram_sync_status": "error",
            },
        )
        return {
            "business_id": business_id,
            "ok": False,
            "reason": "fetch_exception",
            "error": str(e),
        }

    if not isinstance(result, dict):
        logger.error(
            "fetch_follower_count returned non-dict business_id=%s type=%s",
            business_id,
            type(result).__name__,
        )
        _update_business_instagram_fields(
            business_id,
            {
                "instagram_followers_synced_at": timezone.now(),
                "instagram_sync_status": "error",
            },
        )
        return {
            "business_id": business_id,
            "ok": False,
            "reason": "invalid_fetch_result",
        }

    status = result.get("status") or "error"
    count = result.get("follower_count")
    now = timezone.now()

    row_updates = {
        "instagram_followers_synced_at": now,
        "instagram_sync_status": status if status != "skipped_no_token" else "error",
    }
    if status == "ok" and count is not None:
        try:
            row_updates["instagram_follower_count"] = int(count)
        except (TypeError, ValueError, OverflowError) as e:
            logger.warning(
                "Invalid follower_count from Apify business_id=%s raw=%r: %s",
                business_id,
                count,
                e,
            )
            row_updates["instagram_follower_count"] = None
            row_updates["instagram_sync_status"] = "error"
    elif status == "ok":
        logger.warning(
            "Apify reported ok but no follower_count business_id=%s handle=%s",
            business_id,
            normalized,
        )
        row_updates["instagram_follower_count"] = None
        row_updates["instagram_sync_status"] = "error"
    elif status in ("not_found", "private", "error", "skipped_no_token"):
        row_updates["instagram_follower_count"] = None

    n_updated = _update_business_instagram_fields(business_id, row_updates)
    if n_updated > 0 and "instagram_follower_count" in row_updates:
        from quickstart.tasks.search_index_tasks import enqueue_reindex_classes_for_business

        enqueue_reindex_classes_for_business(business_id)
    logger.info(
        "sync_instagram_followers_for_business done business_id=%s handle=%s apify_status=%s "
        "follower_count=%s db_rows_updated=%s stored_sync_status=%s",
        business_id,
        normalized,
        status,
        count,
        n_updated,
        row_updates.get("instagram_sync_status"),
    )
    final_ok = status == "ok" and row_updates.get("instagram_sync_status") == "ok"
    return {
        "business_id": business_id,
        "ok": final_ok,
        "apify_status": status,
        "follower_count": row_updates.get("instagram_follower_count"),
        "instagram_sync_status": row_updates.get("instagram_sync_status"),
        "db_rows_updated": n_updated,
    }


@shared_task
def sync_instagram_followers_for_business(business_id: int) -> dict:
    """Return a small dict so Flower / result backends show what happened (not just None)."""
    try:
        bid = int(business_id)
    except (TypeError, ValueError):
        logger.warning(
            "sync_instagram_followers_for_business invalid business_id=%r",
            business_id,
        )
        return {
            "business_id": business_id,
            "ok": False,
            "reason": "invalid_business_id",
        }

    try:
        return _sync_instagram_followers_core(bid)
    except Exception as e:
        logger.exception(
            "sync_instagram_followers_for_business fatal error business_id=%s",
            bid,
        )
        _update_business_instagram_fields(
            bid,
            {
                "instagram_followers_synced_at": timezone.now(),
                "instagram_sync_status": "error",
            },
        )
        return {
            "business_id": bid,
            "ok": False,
            "reason": "unexpected_error",
            "error": str(e),
        }


def _queue_instagram_for_business_ids(business_ids: list[int]) -> int:
    queued = 0
    for idx, pk in enumerate(business_ids):
        try:
            bid = int(pk)
        except (TypeError, ValueError):
            logger.warning("_queue_instagram_for_business_ids skip invalid pk=%r", pk)
            continue
        try:
            sync_instagram_followers_for_business.apply_async(
                args=[bid],
                countdown=min(idx * 2, 300),
            )
            queued += 1
        except Exception:
            logger.exception(
                "Failed to enqueue sync_instagram_followers_for_business business_id=%s",
                bid,
            )
    return queued


@shared_task
def sync_instagram_followers_all():
    """Fan out per-business sync for businesses with Instagram links and stale sync (beat)."""
    try:
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
        logger.info("sync_instagram_followers_all: queued %s businesses", n)
    except Exception:
        logger.exception("sync_instagram_followers_all failed")


@shared_task
def sync_instagram_followers_enqueue_all():
    """Fan out Instagram follower sync for every business with an Instagram URL (admin manual)."""
    try:
        pks = list(
            BusinessInfo.objects.filter(social_media_links__has_key="instagram")
            .exclude(social_media_links__instagram="")
            .exclude(social_media_links__instagram__isnull=True)
            .values_list("businessId", flat=True)
        )
        n = _queue_instagram_for_business_ids(pks)
        logger.info("sync_instagram_followers_enqueue_all: queued %s businesses", n)
    except Exception:
        logger.exception("sync_instagram_followers_enqueue_all failed")
