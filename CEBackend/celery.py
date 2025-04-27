import os
from celery import Celery
from django.conf import settings # Import Django settings

# Set the default Django settings module for the 'celery' program.
# Replace 'CEBackend.settings' with the actual Python path to your settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'CEBackend.settings')

# Create the Celery application instance
# The first argument is the project name, often the main Django app name.
app = Celery('CEBackend')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix in settings.py.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django app configs.
# Celery will automatically discover tasks defined in files named 'tasks.py'
# within your installed apps.
app.autodiscover_tasks()

# Optional: Example task for debugging
@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')