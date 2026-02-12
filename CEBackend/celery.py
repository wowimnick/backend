import os
import logging
import time

from celery import Celery
from celery.signals import task_failure, worker_ready
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "CEBackend.settings")

app = Celery("CEBackend")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    """In production, prewarm class search cache after worker starts (synchronously so it runs and logs in this process)."""
    if not getattr(settings, "IS_DEPLOYED_ENV", False):
        return
    try:
        from quickstart.utils.cache_prewarm import run_prewarm_class_search_cache
        logger.info("Starting class search cache prewarm...")
        run_prewarm_class_search_cache(locations=True, collections=True, categories=True)
        logger.info("Class search cache prewarm finished.")
    except Exception as e:
        logger.warning("Class search cache prewarm failed: %s", e, exc_info=True)

@task_failure.connect
def handle_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, **kw):
    """
    Catches failed celery tasks and logs them to the cache for dashboard visibility.
    """
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