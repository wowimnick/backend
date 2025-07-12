#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

# --- DYNAMIC ALLOWED_HOSTS CONFIGURATION FOR FARGATE ---
# This block runs only inside an ECS Fargate task.
if [ -n "$ECS_CONTAINER_METADATA_URI_V4" ]; then
    echo "--- Fargate environment detected. Dynamically configuring ALLOWED_HOSTS... ---"
    
    # Use the secure metadata endpoint to get the task's own private IP address.
    # The `jq` tool is used to parse the JSON response.
    TASK_PRIVATE_IP=$(curl -s $ECS_CONTAINER_METADATA_URI_V4 | jq -r '.Networks[0].IPv4Addresses[0]')
    
    if [ -n "$TASK_PRIVATE_IP" ]; then
        # Append the fetched private IP to the existing ALLOWED_HOSTS string.
        # This allows the ALB health check to pass.
        export ALLOWED_HOSTS="${ALLOWED_HOSTS},${TASK_PRIVATE_IP}"
        echo "--- ALLOWED_HOSTS updated to: $ALLOWED_HOSTS ---"
    else
        echo "--- WARNING: Could not determine Fargate task private IP. Health check may fail. ---"
    fi
fi

# --- Standard Startup Procedures ---
echo "--- Running Django migrations ---"
python manage.py migrate --no-input

echo "--- Starting application command ---"
# The 'exec' command replaces the shell process with the command that was passed in (e.g., gunicorn).
# It will inherit the environment variables we've set, including the updated ALLOWED_HOSTS.
exec "$@"