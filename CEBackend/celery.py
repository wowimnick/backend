import os
import logging
import time

from celery import Celery
from celery.signals import task_failure, task_prerun, worker_ready
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections

logger = logging.getLogger(__name__)

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "CEBackend.settings")

app = Celery("CEBackend")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()

# Do not import quickstart.tasks at module level: CEBackend/__init__.py imports this
# module before Django apps are ready (pytest + some tooling). Registration is forced
# in worker_ready and in tests via `import quickstart.tasks`.
# Tasks invoked from views (not listed in CELERY_BEAT_SCHEDULE) must still exist on workers.
_EXTRA_REQUIRED_CELERY_TASKS = frozenset(
    {
        "quickstart.tasks.corporate_booking_tasks.send_shortlist_sent_to_admins",
        "quickstart.tasks.corporate_booking_tasks.send_shortlist_to_corporate",
        "quickstart.tasks.email_marketing_tasks.send_business_marketing_campaign_task",
        "quickstart.tasks.cache_tasks.prewarm_class_search_cache",
        "quickstart.tasks.search_index_tasks.rebuild_all_boundary_buffers_task",
        "quickstart.tasks.search_index_tasks.reindex_dirty_classes_task",
        "quickstart.tasks.search_index_tasks.reindex_class_task",
        "quickstart.tasks.search_index_tasks.rebuild_boundary_buffer_for_id_task",
        "quickstart.tasks.search_index_tasks.bootstrap_typesense_search_index_task",
    }
)


def _required_celery_task_names():
    from django.conf import settings as dj_settings

    beat = getattr(dj_settings, "CELERY_BEAT_SCHEDULE", None) or {}
    names = {entry["task"] for entry in beat.values() if isinstance(entry, dict) and entry.get("task")}
    names |= _EXTRA_REQUIRED_CELERY_TASKS
    return sorted(names)


@task_prerun.connect
def close_stale_db_connections_before_task(**kwargs):
    """Recycle DB conns between tasks (RDS idle timeout does not trigger Django's HTTP hooks)."""
    close_old_connections()


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    """
    Fail fast if worker image is missing beat-scheduled or critical .delay() tasks (deploy skew).
    Operational fix: redeploy Celery workers with the same image tag as the web tier.
    """
    import quickstart.tasks  # noqa: F401 — registers submodules after Django setup

    missing = [name for name in _required_celery_task_names() if name not in app.tasks]
    if missing:
        raise RuntimeError(
            "Celery worker is missing registered tasks (redeploy worker with current code): "
            + ", ".join(missing)
        )

    try:
        from quickstart.tasks.search_index_tasks import enqueue_typesense_bootstrap_check

        enqueue_typesense_bootstrap_check()
        logger.info("Scheduled Typesense bootstrap check (if TYPESENSE_AUTO_BOOTSTRAP).")
    except Exception as e:
        logger.warning(
            "Could not schedule Typesense bootstrap check: %s", e, exc_info=True
        )

    # In production, prewarm class search shortly after worker is ready. Running this
    # synchronously in worker_ready held the boot sequence open (many /api/classes/search/
    # requests) and overlapped with deploy SIGTERM, causing WorkerLost noise and failed tasks.
    if not getattr(settings, "IS_DEPLOYED_ENV", False):
        return
    try:
        from quickstart.tasks.cache_tasks import prewarm_class_search_cache_task

        prewarm_class_search_cache_task.apply_async(countdown=8)
        logger.info("Scheduled class search cache prewarm (apply_async countdown=8).")
    except Exception as e:
        logger.warning("Could not schedule class search cache prewarm: %s", e, exc_info=True)


@task_failure.connect
def handle_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, **kw):
    """
    Catches failed celery tasks and logs them to the cache for dashboard visibility.
    """
    # Worker shutdown / SIGTERM during deploy: not an app bug; skip noisy failure cache entries.
    if exception is not None:
        name = exception.__class__.__name__
        msg = str(exception)
        if name in ("WorkerLostError", "WorkerShutdown") or "SIGTERM" in msg or "WorkerShutdown" in msg:
            logger.info(
                "Celery task %s [%s] ended with worker shutdown (ignored for failure dashboard): %s",
                getattr(sender, "name", sender),
                task_id,
                msg,
            )
            return

    logger.error(f"Celery task {sender.name} [{task_id}] failed: {exception}")
    
    # Structure the error info
    error_info = {
        'timestamp': time.time(),
        'task_name': sender.name,
        'task_id': task_id,
        'exception_type': exception.__class__.__name__,
        'exception_message': str(exception),
        'args': str(args),
        'kwargs': str(kwargs),
        'traceback': str(traceback)
    }

    try:
        # Store the last N failed tasks (e.g., 20) in cache
        failed_tasks = cache.get('celery_failed_tasks', [])
        failed_tasks.insert(0, error_info) # Add to the beginning
        # Store for 1 day
        cache.set('celery_failed_tasks', failed_tasks[:20], timeout=86400)
    except Exception as e:
        logger.error(f"Could not log celery task failure to cache: {e}")


# Optional: Example task for debugging
@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')