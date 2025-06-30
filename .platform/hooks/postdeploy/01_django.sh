#!/bin/bash

# This script runs during the 'postdeploy' hook on Amazon Linux 2023.
# It runs after the application version is deployed and started.

set -e # Exit immediately if a command exits with a non-zero status.

# Use the leader_only helper to ensure commands run only on one instance
# in a multi-instance environment. This is crucial for migrations.
if /usr/bin/leader_only; then
  echo "--- I am the leader, running database migrations ---"
  
  # NO NEED TO SOURCE A VIRTUALENV ON AL2023.
  # The python executable is already in the PATH.
  python manage.py migrate --noinput
  
  echo "--- Migrations complete ---"
else
  echo "--- I am not the leader, skipping migrations ---"
fi