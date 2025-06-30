web: gunicorn -w 3 -k uvicorn.workers.UvicornWorker CEBackend.asgi:application
celery: celery -A CEBackend worker -l INFO --concurrency=4