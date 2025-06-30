#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

echo "--- Running Django migrations ---"
python manage.py migrate --no-input

# The "$@" means "execute the command that was passed to this script".
# This allows us to use the same image for the web server and celery worker
# by passing different commands (e.g., gunicorn vs. celery).
echo "--- Starting application command ---"
exec "$@"