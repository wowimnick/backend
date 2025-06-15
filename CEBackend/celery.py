import os
from celery import Celery
from celery.signals import task_failure 
from django.conf import settings
from django.core.cache import cache 
import time
import logging

logger = logging.getLogger(__name__)

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'CEBackend.settings')

app = Celery('CEBackend')

app.config_from_object('django.conf:settings', namespace='CELERY')

app.autodiscover_tasks()

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