#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

echo "--- Running Django migrations ---"
python manage.py migrate --no-input

echo "--- Collecting static files to S3 ---"
# This will upload static files to your S3 bucket as configured in settings.py
python manage.py collectstatic --no-input --clear

# Execute the command passed to this script (e.g., gunicorn or celery)
exec "$@"