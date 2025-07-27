#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

# --- DYNAMIC ALLOWED_HOSTS CONFIGURATION FOR FARGATE ---
if [ -n "$ECS_CONTAINER_METADATA_URI_V4" ]; then
    echo "--- Fargate environment detected. Dynamically configuring ALLOWED_HOSTS... ---"
    
    TASK_PRIVATE_IP=$(curl -s $ECS_CONTAINER_METADATA_URI_V4 | jq -r '.Networks[0].IPv4Addresses[0]')
    
    if [ -n "$TASK_PRIVATE_IP" ]; then
        export ALLOWED_HOSTS="${ALLOWED_HOSTS},${TASK_PRIVATE_IP}"
        echo "--- ALLOWED_HOSTS updated to: $ALLOWED_HOSTS ---"
    else
        echo "--- WARNING: Could not determine Fargate task private IP. ---"
    fi
fi

# --- NEW: ROLE-BASED STARTUP LOGIC ---
# The CONTAINER_ROLE variable should be set in your ECS Task Definition.

if [ "$CONTAINER_ROLE" = "web" ]; then
    # If this is the web container, run migrations before starting the server.
    # This prevents the worker from trying to run migrations at the same time.
    echo "--- [WEB] Running Django migrations ---"
    python manage.py migrate --no-input

    echo "--- [WEB] Starting Gunicorn server ---"
    # The 'exec "$@"' will run the CMD from the ECS Task Definition (e.g., gunicorn)
    exec "$@"

elif [ "$CONTAINER_ROLE" = "worker" ]; then
    # If this is the worker container, do NOT run migrations.
    # Just start supervisord, which will handle the worker and beat processes.
    echo "--- [WORKER] Starting Supervisor... ---"
    # The 'exec "$@"' will run the CMD from the ECS Task Definition (e.g., supervisord)
    exec "$@"

else
    echo "--- ERROR: CONTAINER_ROLE is not set or is invalid. ---"
    echo "--- Please set CONTAINER_ROLE to 'web' or 'worker'. ---"
    exit 1
fi