#!/bin/bash

# This script runs after the application is deployed, ensuring environment variables are available.
set -e # Exit immediately if a command fails

# The 'leader_only' helper ensures these commands run on only one instance.
if /opt/elasticbeanstalk/bin/leader_only; then
  echo "--- I am the leader, running post-deployment commands ---"
  
  # Activate the virtual environment.
  # The path is consistent on Amazon Linux 2023 platforms.
  source /var/app/venv/staging-LQM1lest/bin/activate
  
  echo "--- Running database migrations ---"
  python manage.py migrate --noinput
  
  echo "--- Collecting static files ---"
  python manage.py collectstatic --noinput
  
else
  echo "--- I am not the leader, skipping post-deployment commands ---"
fi