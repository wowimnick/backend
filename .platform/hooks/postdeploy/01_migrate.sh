#!/bin/bash

# This script runs after the application has been deployed and the environment
# variables are available. It is the reliable way to run database migrations.

# The 'set -e' command ensures that the script will exit immediately if a command fails.
set -e

# We use the 'leader_only' helper provided by Elastic Beanstalk to ensure
# that this script only runs on one instance in an environment.
# This is critical for preventing multiple instances from trying to run
# migrations at the same time.
if /opt/elasticbeanstalk/bin/leader_only; then
  echo "--- I am the leader, running migrations ---"
  # Activate the virtual environment
  source /var/app/venv/staging-LQM1lest/bin/activate
  # Run the migrate command
  python manage.py migrate --noinput
else
  echo "--- I am not the leader, skipping migrations ---"
fi