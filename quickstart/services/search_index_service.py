"""Build Typesense documents from Postgres and upsert/delete."""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone

from quickstart.models import (
    BusinessInfo,
    ClassCollection,
    ClassImage,
    ClassesMain,
    Schedule,
    ScheduleInstance,
)
from quickstart.serializers.public.public_class_serializers import (
    PublicClassOptionSerializer,
)
from quickstart.services.search_ranking import compute_search_relevance_score
from quickstart.services.search_schema import CLASS_SEARCH_SCHEMA_BODY
from quickstart.services.typesense_client import get_typesense_client
from quickstart.utils.deploy_build_id import get_deploy_build_id
from quickstart.utils.url_utils import build_cloudfront_resized_webp_from_original_key

logger = logging.getLogger(__name__)

WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Date–time correlation facet tokens (avoid '|': Typesense filter syntax treats '|' as OR outside quotes).
AVAILABILITY_SLOT_SEP = "::"


def date_to_yyyymmdd(d) -> int:
    """Compact date as YYYYMMDD int (sorts/compares correctly for search filters)."""
    return d.year * 10000 + d.month * 100 + d.day


def format_availability_slot(date_iso: str, bucket_label: str) -> str:
    return f"{date_iso}{AVAILABILITY_SLOT_SEP}{bucket_label}"

TIME_BUCKET_LABELS = {
    "morning": "Morning (6am-12pm)",
    "afternoon": "Afternoon (12pm-5pm)",
    "evening": "Evening (5pm-10pm)",
}


def instance_time_bucket_label(t) -> str | None:
    """Match PublicClassViewSet._public_class_search_database time_ranges (time-of-day filter)."""
    from datetime import time as dt_time

    ranges = (
        (TIME_BUCKET_LABELS["morning"], dt_time(6, 0), dt_time(11, 59, 59)),
        (TIME_BUCKET_LABELS["afternoon"], dt_time(12, 0), dt_time(16, 59, 59)),
        (TIME_BUCKET_LABELS["evening"], dt_time(17, 0), dt_time(21, 59, 59)),
    )
    for label, start, end in ranges:
        if start <= t <= end:
            return label
    return None


def _image_medium_dict(img: ClassImage) -> dict[str, Any]:
    image_key = img.image.name if img.image and img.image.name else None
    medium_url = (
        build_cloudfront_resized_webp_from_original_key(image_key, "medium")
        if image_key
        else None
    )
    return {
        "imageId": img.imageId,
        "image_key": image_key,
        "medium_url": medium_url,
        "isCover": img.isCover,
    }


def _combined_rating_counts(klass: ClassesMain, business: BusinessInfo) -> tuple[Decimal, int]:
    p_count = int(klass.platform_review_count or 0)
    p_rating = Decimal(str(klass.platform_avg_rating or "0"))
    g_count = int(business.google_review_count or 0)
    g_rating = Decimal(str(business.google_avg_rating or "0"))
    total_reviews = p_count + g_count
    if total_reviews == 0:
        return Decimal("0.0"), 0
    total_rating_sum = (p_rating * p_count) + (g_rating * g_count)
    combined_avg = total_rating_sum / Decimal(total_reviews)
    return round(combined_avg, 1), total_reviews


def _deterministic_jitter_coord(klass: ClassesMain) -> tuple[float, float] | None:
    if klass.point is None:
        return None
    lat, lng = float(klass.point.y), float(klass.point.x)
    if klass.saltLocation:
        h = hash(str(klass.classId))
        lat += ((h % 1000) / 1000.0 - 0.5) * 0.001
        lng += (((h // 1000) % 1000) / 1000.0 - 0.5) * 0.001
    return lat, lng


def review_count_with_offset(class_id: int, platform_count: int, google_count: int) -> int:
    original = platform_count + google_count
    if original == 0:
        return 0
    py_hash = hash(str(class_id))
    offset = 10 + (abs(py_hash) % 11)
    return original + offset


def build_typesense_document_for_class(class_id: int) -> dict[str, Any] | None:
    today = timezone.now().date()
    try:
        klass = (
            ClassesMain.objects.select_related("businessId", "location_ref")
            .prefetch_related(
                Prefetch(
                    "collections",
                    queryset=ClassCollection.objects.filter(is_active=True).select_related(
                        "parent"
                    ),
                ),
                "images",
                "options",
            )
            .get(pk=class_id)
        )
    except ClassesMain.DoesNotExist:
        return None

    business = klass.businessId
    if (
        klass.status != "active"
        or not business.isActive
        or business.verificationStatus != "verified"
    ):
        return None

    has_future = ScheduleInstance.objects.filter(
        schedule__option__classId=klass,
        date__gte=today,
        status="scheduled",
    ).exists()
    if not has_future:
        return None

    min_sess = (
        Schedule.objects.filter(
            option__classId=klass,
            option__booking_type="Single Session",
            date__gte=today,
        )
        .order_by("price")
        .values_list("price", flat=True)
        .first()
    )
    min_course = (
        Schedule.objects.filter(
            option__classId=klass,
            option__booking_type="Full Course",
            end_date__gte=today,
        )
        .order_by("price")
        .values_list("price", flat=True)
        .first()
    )
    min_sess_d = float(min_sess) if min_sess is not None else None
    min_course_d = float(min_course) if min_course is not None else None
    candidates = [x for x in (min_sess_d, min_course_d) if x is not None]
    min_price = min(candidates) if candidates else None

    instances = list(
        ScheduleInstance.objects.filter(
            schedule__option__classId=klass,
            date__gte=today,
            status="scheduled",
        ).select_related("schedule")
    )
    avail_dates = sorted({i.date.isoformat() for i in instances})[:180]
    max_avail = max(i.date for i in instances)
    max_available_date_compact = date_to_yyyymmdd(max_avail)

    time_buckets: set[str] = set()
    availability_slots: set[str] = set()
    weekdays: set[str] = set()
    max_cap = 0
    durations: list[int] = []
    for i in instances:
        weekdays.add(WEEKDAY_ABBR[i.date.weekday()])
        label = instance_time_bucket_label(i.time)
        if label:
            time_buckets.add(label)
            availability_slots.add(format_availability_slot(i.date.isoformat(), label))
        max_cap = max(max_cap, i.max_participants)
        durations.append(i.duration)

    listing_duration = min(durations) if durations else None

    collection_slugs = []
    collection_paths = []
    for c in klass.collections.all():
        collection_slugs.append(c.slug)
        if c.parent_id:
            collection_paths.append(f"{c.parent.slug}/{c.slug}")
        else:
            collection_paths.append(c.slug)

    option_tags: set[str] = set()
    booking_types_set: set[str] = set()
    options_payload = []
    for opt in klass.options.all():
        ser = PublicClassOptionSerializer(opt).data
        options_payload.append(ser)
        if getattr(opt, "booking_type", None):
            booking_types_set.add(str(opt.booking_type))
        for t in opt.tags or []:
            if t:
                option_tags.add(str(t).lower())

    images_qs = sorted(
        list(klass.images.all()),
        key=lambda im: (
            not im.isCover,
            -(im.createdAt.timestamp() if im.createdAt else 0),
        ),
    )
    image_payload = [_image_medium_dict(im) for im in images_qs]

    combined_avg, combined_raw = _combined_rating_counts(klass, business)

    image_count = klass.images.count()
    relevance = compute_search_relevance_score(
        created_at=klass.createdAt,
        description_len=len(klass.description or ""),
        image_count=image_count,
        average_rating=combined_avg,
        review_count=combined_raw,
        business_featured=bool(business.featured),
    )

    jitter = _deterministic_jitter_coord(klass)
    if jitter is None:
        return None
    lat_t, lng_t = jitter

    loc_name = klass.location_ref.name if klass.location_ref else None
    loc_display = klass.location
    if klass.saltLocation:
        parts = [p for p in [klass.city, klass.state] if p]
        loc_display = ", ".join(parts) if parts else None

    review_display = review_count_with_offset(
        klass.classId,
        int(klass.platform_review_count or 0),
        int(business.google_review_count or 0),
    )

    card = {
        "classId": klass.classId,
        "slug": klass.slug,
        "business_slug": business.slug,
        "businessId": business.businessId,
        "business_name": business.businessName,
        "title": klass.title,
        "description": klass.description,
        "features": klass.features,
        "location": loc_display,
        "location_name": loc_name,
        "unit_number": klass.unit_number or None,
        "coordinates": f"{lat_t:.8f},{lng_t:.8f}",
        "saltLocation": klass.saltLocation,
        "createdAt": klass.createdAt.isoformat().replace("+00:00", "Z")
        if timezone.is_aware(klass.createdAt)
        else klass.createdAt.isoformat() + "Z",
        "options": options_payload,
        "images": image_payload,
        "average_rating": float(combined_avg),
        "review_count": review_display,
        "is_favorited": False,
        "business_timezone": business.business_timezone,
        "city": klass.city,
        "state": klass.state,
        "min_session_price": str(min_sess) if min_sess is not None else None,
        "min_course_price": str(min_course) if min_course is not None else None,
        "soonest_next_week": None,
        "listing_duration_minutes": listing_duration,
        "student_contact_email": None,
        "student_contact_phone": None,
        "require_participant_names": bool(business.require_participant_names),
    }
    privacy = getattr(business, "contact_privacy", None)
    if privacy in ("public", "public_with_chat"):
        card["student_contact_email"] = business.studentContactEmail or None
        card["student_contact_phone"] = business.studentContactPhone or None

    min_price_val = min_price
    price_unknown = min_price is None

    doc = {
        "id": str(klass.classId),
        "class_id": klass.classId,
        "slug": klass.slug,
        "title": klass.title,
        "description": klass.description or "",
        "business_name": business.businessName or "",
        "business_state": (business.businessState or "").strip(),
        "business_featured": bool(business.featured),
        "location": [lat_t, lng_t],
        "collection_slugs": collection_slugs,
        "collection_paths": collection_paths,
        "option_tags": sorted(option_tags),
        "booking_types": sorted(booking_types_set),
        "min_price": min_price_val,
        "price_unknown": price_unknown,
        "min_session_price": min_sess_d,
        "min_course_price": min_course_d,
        "available_dates": avail_dates,
        "max_available_date": max_available_date_compact,
        "time_buckets": sorted(time_buckets),
        "availability_slots": sorted(availability_slots),
        "weekdays": sorted(weekdays),
        "max_capacity": int(max_cap),
        "relevance_score": relevance,
        "created_at_ts": int(klass.createdAt.timestamp()),
        "combined_avg_rating": float(combined_avg),
        "combined_review_count_raw": int(combined_raw),
        "listing_duration_minutes": listing_duration or 0,
        "card_json": json.dumps(card, default=str),
    }
    return doc


def typesense_max_avail_filter_cache_key(physical_collection: str) -> str:
    return f"typesense_max_avail_ok:{physical_collection}"


def ensure_physical_collection(name: str) -> None:
    client = get_typesense_client()
    if not client:
        raise RuntimeError("Typesense client not configured")
    body = dict(CLASS_SEARCH_SCHEMA_BODY)
    body["name"] = name
    try:
        existing = client.collections[name].retrieve()
        existing_fields = {f["name"] for f in existing.get("fields", [])}
        missing = [f for f in body["fields"] if f["name"] not in existing_fields]
        if missing:
            client.collections[name].update({"fields": missing})
            try:
                cache.delete(typesense_max_avail_filter_cache_key(name))
            except Exception:
                pass
            if any(f.get("name") == "max_available_date" for f in missing):
                try:
                    if cache.add("typesense_reconcile_after_max_avail_schema", 1, timeout=3600):
                        from quickstart.tasks.search_index_tasks import (
                            reconcile_typesense_classes_task,
                        )

                        reconcile_typesense_classes_task.delay()
                except Exception as e:
                    logger.warning(
                        "Could not enqueue reconcile after max_available_date schema patch: %s",
                        e,
                        exc_info=True,
                    )
    except Exception:
        client.collections.create(body)


def upsert_alias_to_collection(alias_name: str, physical_name: str) -> None:
    client = get_typesense_client()
    if not client:
        raise RuntimeError("Typesense client not configured")
    client.aliases.upsert(alias_name, {"collection_name": physical_name})


def import_documents(physical_collection: str, docs: list[dict[str, Any]]) -> None:
    if not docs:
        return
    client = get_typesense_client()
    if not client:
        raise RuntimeError("Typesense client not configured")
    ensure_physical_collection(physical_collection)
    jsonl = "\n".join(json.dumps(d, default=str) for d in docs)
    client.collections[physical_collection].documents.import_(jsonl, {"action": "upsert"})


def delete_document(physical_or_alias: str, class_id: int) -> None:
    client = get_typesense_client()
    if not client:
        return
    try:
        client.collections[physical_or_alias].documents[str(class_id)].delete()
    except Exception as e:
        logger.debug("Typesense delete %s: %s", class_id, e)


def index_single_class(class_id: int) -> None:
    doc = build_typesense_document_for_class(class_id)
    alias = getattr(settings, "TYPESENSE_COLLECTION_ALIAS", "classes_live")
    client = get_typesense_client()
    if not client:
        return
    try:
        physical = client.aliases[alias].retrieve()["collection_name"]
    except Exception:
        physical = alias
    if doc is None:
        delete_document(physical, class_id)
        ClassesMain.objects.filter(pk=class_id).update(search_relevance_score=0.0)
    else:
        ensure_physical_collection(physical)
        client.collections[physical].documents.upsert(doc)
        ClassesMain.objects.filter(pk=class_id).update(
            search_relevance_score=doc["relevance_score"]
        )


def indexable_classes_base_queryset():
    today = timezone.now().date()
    return ClassesMain.objects.filter(
        status="active",
        businessId__isActive=True,
        businessId__verificationStatus="verified",
    ).filter(
        Exists(
            ScheduleInstance.objects.filter(
                schedule__option__classId=OuterRef("pk"),
                date__gte=today,
                status="scheduled",
            )
        )
    )


def count_indexable_classes() -> int:
    return indexable_classes_base_queryset().count()


def iter_all_indexable_class_ids():
    return indexable_classes_base_queryset().values_list("classId", flat=True)


def typesense_alias_physical_document_count() -> tuple[int | None, str | None]:
    """Return (num_documents, physical_collection_name) or (None, None) if alias/collection missing."""
    client = get_typesense_client()
    if not client:
        return None, None
    alias = getattr(settings, "TYPESENSE_COLLECTION_ALIAS", "classes_live")
    try:
        meta = client.aliases[alias].retrieve()
        physical = meta["collection_name"]
        coll = client.collections[physical].retrieve()
        raw = coll.get("num_documents")
        return (int(raw) if raw is not None else 0, physical)
    except Exception as e:
        logger.debug("typesense_alias_physical_document_count: %s", e)
        return None, None


def needs_typesense_full_reindex() -> tuple[bool, str]:
    from quickstart.services.typesense_client import typesense_available

    if not typesense_available():
        return False, "typesense not configured"
    db_n = count_indexable_classes()
    ts_n, _physical = typesense_alias_physical_document_count()
    if ts_n is None:
        return True, "alias or collection missing / unreachable"
    if db_n > 0 and ts_n == 0:
        return True, "typesense has 0 documents but DB has indexable classes"
    return False, "index looks healthy"


# Redis lock shared with Celery bootstrap_typesense_search_index_task.
BOOTSTRAP_LOCK_KEY = "typesense_bootstrap_job_lock"
BOOTSTRAP_LOCK_TTL = 7200

_TYPESENSE_FULL_REINDEX_BUILD_KEY_PREFIX = "typesense_full_reindexed_build"
_TYPESENSE_FULL_REINDEX_BUILD_TTL = 30 * 24 * 3600  # 30 days (same idea as clear_public_caches)


def execute_typesense_bootstrap_if_needed() -> bool:
    """If the index is missing or empty, run a full reindex. Caller must hold BOOTSTRAP_LOCK_KEY."""
    need, reason = needs_typesense_full_reindex()
    if not need:
        return False
    logger.warning("Typesense bootstrap executing (%s)", reason)
    run_full_typesense_reindex()
    return True


def sync_typesense_bootstrap_at_web_startup(
    *, max_wait_peer_seconds: int = 3600, poll_seconds: float = 4.0
) -> None:
    """
    Block until Typesense has a usable search index (or timeout).

    Runs on web container startup so search works without waiting for Celery.
    Uses BOOTSTRAP_LOCK_KEY so multiple web tasks / Celery do not double-reindex.
    """
    from django.core.cache import cache

    from quickstart.services.typesense_client import typesense_available

    if not getattr(settings, "TYPESENSE_AUTO_BOOTSTRAP", False):
        return
    if not typesense_available():
        return
    need, _reason = needs_typesense_full_reindex()
    if not need:
        return

    if cache.add(BOOTSTRAP_LOCK_KEY, 1, timeout=BOOTSTRAP_LOCK_TTL):
        try:
            execute_typesense_bootstrap_if_needed()
        except Exception as e:
            logger.warning("Typesense startup bootstrap failed: %s", e, exc_info=True)
        finally:
            cache.delete(BOOTSTRAP_LOCK_KEY)
        return

    logger.info("Typesense startup: waiting for peer bootstrap lock...")
    deadline = time.monotonic() + max_wait_peer_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        need2, _ = needs_typesense_full_reindex()
        if not need2:
            logger.info("Typesense startup: index ready (peer completed)")
            return
    logger.warning(
        "Typesense startup: timed out after %ss waiting for index",
        max_wait_peer_seconds,
    )


def sync_typesense_full_reindex_once_per_build(
    *,
    max_wait_peer_seconds: int = 3600,
    poll_seconds: float = 4.0,
) -> None:
    """
    Run a full Typesense reindex once per deploy build id (BUILD_ID / IMAGE_TAG / GIT_SHA).

    Uses the same Redis lock as bootstrap so Celery cannot overlap; sets a build marker so scaled-out
    web tasks skip. Waits on the marker if another task holds the lock.
    """
    from django.core.cache import cache

    from quickstart.services.typesense_client import typesense_available

    if not getattr(settings, "TYPESENSE_AUTO_BOOTSTRAP", False):
        return
    if not typesense_available():
        return

    build_id = get_deploy_build_id()
    env = getattr(settings, "DJANGO_ENV", "local")
    if not build_id:
        logger.warning(
            "Typesense full reindex each deploy requires BUILD_ID, IMAGE_TAG, or GIT_SHA; "
            "falling back to bootstrap-only startup sync."
        )
        sync_typesense_bootstrap_at_web_startup(
            max_wait_peer_seconds=max_wait_peer_seconds,
            poll_seconds=poll_seconds,
        )
        return

    marker_key = f"{_TYPESENSE_FULL_REINDEX_BUILD_KEY_PREFIX}:{env}:{build_id}"
    try:
        if cache.get(marker_key):
            logger.info(
                "Typesense deploy full reindex skipped (already completed for build=%s)",
                build_id,
            )
            return
    except Exception as e:
        logger.warning("Typesense deploy sync: could not read build marker: %s", e)
        return

    if cache.add(BOOTSTRAP_LOCK_KEY, 1, timeout=BOOTSTRAP_LOCK_TTL):
        try:
            logger.warning("Typesense deploy full reindex starting (build=%s)", build_id)
            run_full_typesense_reindex()
            try:
                cache.set(marker_key, "1", timeout=_TYPESENSE_FULL_REINDEX_BUILD_TTL)
            except Exception as e:
                logger.warning("Typesense deploy sync: could not set build marker: %s", e)
        except Exception as e:
            logger.warning(
                "Typesense deploy full reindex failed: %s",
                e,
                exc_info=True,
            )
        finally:
            cache.delete(BOOTSTRAP_LOCK_KEY)
        return

    logger.info("Typesense deploy sync: waiting for peer full reindex (build=%s)...", build_id)
    deadline = time.monotonic() + max_wait_peer_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        try:
            if cache.get(marker_key):
                logger.info("Typesense deploy full reindex complete (peer marker seen)")
                return
        except Exception:
            pass
        need2, _ = needs_typesense_full_reindex()
        if not need2:
            logger.info("Typesense deploy sync: index ready (peer completed)")
            return

    need3, _ = needs_typesense_full_reindex()
    if not need3:
        logger.info(
            "Typesense deploy sync: index healthy after waiting for peer (marker timeout)"
        )
        return
    logger.warning(
        "Typesense deploy sync: timed out after %ss waiting for full reindex marker",
        max_wait_peer_seconds,
    )


def sync_typesense_at_web_deploy_startup(
    *,
    max_wait_peer_seconds: int = 3600,
    poll_seconds: float = 4.0,
) -> None:
    """Web entrypoint: full reindex each deploy when enabled, else bootstrap-if-needed only."""
    from quickstart.services.typesense_client import typesense_available

    if not getattr(settings, "TYPESENSE_AUTO_BOOTSTRAP", False):
        return
    if not typesense_available():
        return

    if getattr(settings, "TYPESENSE_FULL_REINDEX_EACH_DEPLOY", True):
        sync_typesense_full_reindex_once_per_build(
            max_wait_peer_seconds=max_wait_peer_seconds,
            poll_seconds=poll_seconds,
        )
    else:
        sync_typesense_bootstrap_at_web_startup(
            max_wait_peer_seconds=max_wait_peer_seconds,
            poll_seconds=poll_seconds,
        )


def run_full_typesense_reindex(batch_size: int = 200) -> dict[str, Any]:
    """Create a new physical collection, import all indexable docs, swap alias (same as reindex_classes)."""
    client = get_typesense_client()
    if not client:
        raise RuntimeError("Typesense client not configured")
    batch = max(10, int(batch_size))
    alias = getattr(settings, "TYPESENSE_COLLECTION_ALIAS", "classes_live")
    physical = f"{alias}_v{int(time.time())}"
    logger.info("Typesense full reindex: creating physical collection %s", physical)
    ensure_physical_collection(physical)
    ids = list(iter_all_indexable_class_ids())
    buf: list[dict[str, Any]] = []
    indexed = 0
    for i, cid in enumerate(ids, start=1):
        doc = build_typesense_document_for_class(cid)
        if doc:
            buf.append(doc)
            indexed += 1
        if len(buf) >= batch:
            import_documents(physical, buf)
            buf = []
        if i % 2000 == 0:
            logger.info("Typesense full reindex progress %s/%s", i, len(ids))
    if buf:
        import_documents(physical, buf)
    upsert_alias_to_collection(alias, physical)
    try:
        cache.set(typesense_max_avail_filter_cache_key(physical), 1, timeout=7 * 86400)
    except Exception as e:
        logger.debug("typesense max_avail cache set failed: %s", e)
    logger.info(
        "Typesense full reindex complete alias=%s physical=%s indexed=%s scanned=%s",
        alias,
        physical,
        indexed,
        len(ids),
    )
    return {
        "alias": alias,
        "physical": physical,
        "indexed": indexed,
        "scanned": len(ids),
    }
