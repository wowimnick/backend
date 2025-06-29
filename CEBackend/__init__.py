import sys

# This will import the Celery app instance and discover tasks
if "test" not in sys.argv:
    from .celery import app as celery_app

    __all__ = ("celery_app",)
