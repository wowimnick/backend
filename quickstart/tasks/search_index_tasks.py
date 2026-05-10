"""Background indexing for Typesense public class search."""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def reindex_class_task(class_id: int):
    """Upsert or delete one class document in Typesense."""
    try:
        from quickstart.services.search_index_service import index_single_class

        index_single_class(int(class_id))
    except Exception as e:
        logger.warning("reindex_class_task failed for %s: %s", class_id, e, exc_info=True)


@shared_task(ignore_result=True)
def rebuild_boundary_buffer_for_id_task(boundary_id: str):
    from uuid import UUID

    from quickstart.services.boundary_geometry_service import rebuild_boundary_polygon_cache

    try:
        rebuild_boundary_polygon_cache(UUID(str(boundary_id)))
    except Exception as e:
        logger.warning(
            "rebuild_boundary_buffer_for_id_task failed for %s: %s",
            boundary_id,
            e,
            exc_info=True,
        )


@shared_task(ignore_result=True)
def rebuild_all_boundary_buffers_task():
    from quickstart.services.boundary_geometry_service import rebuild_all_boundary_polygon_caches

    try:
        rebuild_all_boundary_polygon_caches()
    except Exception as e:
        logger.warning(
            "rebuild_all_boundary_buffers_task failed: %s", e, exc_info=True
        )


@shared_task(ignore_result=True)
def reconcile_typesense_classes_task():
    """
    Daily sweep: upsert or delete each active verified class in Typesense so docs
    match "has future scheduled instance" after calendar rolls (no DB write).
    """
    from quickstart.models import ClassesMain
    from quickstart.services.search_index_service import index_single_class

    ids = ClassesMain.objects.filter(
        status="active",
        businessId__isActive=True,
        businessId__verificationStatus="verified",
    ).values_list("classId", flat=True)
    for cid in ids.iterator(chunk_size=500):
        try:
            index_single_class(int(cid))
        except Exception as e:
            logger.debug("reconcile_typesense_classes_task skip %s: %s", cid, e)


@shared_task(ignore_result=True)
def reindex_dirty_classes_task():
    """Re-push rows touched recently so Typesense stays near-real-time."""
    from datetime import timedelta

    from django.utils import timezone

    from quickstart.models import ClassesMain
    from quickstart.services.search_index_service import index_single_class

    cutoff = timezone.now() - timedelta(hours=3)
    ids = (
        ClassesMain.objects.filter(updatedAt__gte=cutoff)
        .values_list("classId", flat=True)[:2000]
    )
    for cid in ids:
        try:
            index_single_class(cid)
        except Exception as e:
            logger.debug("reindex_dirty skip %s: %s", cid, e)


def enqueue_reindex_class(class_id: int) -> None:
    """Schedule reindex after the surrounding transaction commits."""

    def _run():
        try:
            reindex_class_task.delay(int(class_id))
        except Exception as e:
            logger.warning("enqueue_reindex_class failed for %s: %s", class_id, e)

    transaction.on_commit(_run)


def enqueue_reindex_classes_for_option(option_id: int) -> None:
    from quickstart.models import ClassOption

    try:
        oid = int(option_id)
        cid = (
            ClassOption.objects.filter(pk=oid).values_list("classId_id", flat=True).first()
        )
        if cid:
            enqueue_reindex_class(int(cid))
    except Exception as e:
        logger.debug("enqueue_reindex_classes_for_option: %s", e)


def enqueue_reindex_classes_for_schedule(schedule_id: int) -> None:
    from quickstart.models import Schedule

    try:
        sid = int(schedule_id)
        cid = (
            Schedule.objects.filter(pk=sid)
            .values_list("option__classId_id", flat=True)
            .first()
        )
        if cid:
            enqueue_reindex_class(int(cid))
    except Exception as e:
        logger.debug("enqueue_reindex_classes_for_schedule: %s", e)


def enqueue_reindex_classes_for_instance(instance_id: int) -> None:
    from quickstart.models import ScheduleInstance

    try:
        iid = int(instance_id)
        cid = (
            ScheduleInstance.objects.filter(pk=iid)
            .values_list("schedule__option__classId_id", flat=True)
            .first()
        )
        if cid:
            enqueue_reindex_class(int(cid))
    except Exception as e:
        logger.debug("enqueue_reindex_classes_for_instance: %s", e)


@shared_task(ignore_result=True)
def bootstrap_typesense_search_index_task():
    """Create/populate Typesense when alias is missing or index is empty but DB has classes."""
    from django.conf import settings
    from django.core.cache import cache

    from quickstart.services.search_index_service import (
        BOOTSTRAP_LOCK_TTL,
        execute_typesense_bootstrap_if_needed,
        typesense_bootstrap_lock_cache_key,
    )
    from quickstart.services.typesense_client import typesense_available

    if not getattr(settings, "TYPESENSE_AUTO_BOOTSTRAP", False):
        return
    if not typesense_available():
        return
    lock_key = typesense_bootstrap_lock_cache_key()
    if not cache.add(lock_key, 1, timeout=BOOTSTRAP_LOCK_TTL):
        logger.info("typesense bootstrap skipped (another job holds the lock)")
        return
    try:
        if not execute_typesense_bootstrap_if_needed():
            logger.debug("typesense bootstrap not needed")
    except Exception as e:
        logger.warning("typesense bootstrap failed: %s", e, exc_info=True)
    finally:
        cache.delete(lock_key)


def enqueue_typesense_bootstrap_check(delay: int | None = None) -> None:
    from django.conf import settings

    from quickstart.services.typesense_client import typesense_available

    if not getattr(settings, "TYPESENSE_AUTO_BOOTSTRAP", False):
        return
    if not typesense_available():
        return
    d = delay if delay is not None else getattr(
        settings, "TYPESENSE_BOOTSTRAP_DELAY_SECONDS", 90
    )
    try:
        bootstrap_typesense_search_index_task.apply_async(countdown=int(d))
    except Exception as e:
        logger.warning("enqueue_typesense_bootstrap_check failed: %s", e, exc_info=True)
