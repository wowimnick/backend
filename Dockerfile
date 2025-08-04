# Use a specific, stable Python version
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=off
ENV PIP_DISABLE_PIP_VERSION_CHECK=on

# Set the working directory
WORKDIR /home/django/app

# Install system dependencies
# MODIFIED: Added gdal-bin and libgdal-dev for geospatial support
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    jq \
    curl \
    supervisor \
    gdal-bin \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file
COPY requirements.txt .

# Install Python dependencies
# The psycopg2 and GDAL python packages will now build correctly
# because libpq-dev and libgdal-dev are installed.
RUN pip install -r requirements.txt

# Create a non-root user for security
RUN groupadd -r django && useradd -r -g django django

# Copy supervisor config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Copy the rest of the application code
COPY . .

# Copy the entrypoint script and make it executable
COPY ./entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Change ownership of the app directory.
# Also create log directory and set its permissions.
RUN mkdir -p /home/django/app/logs && \
    chown -R django:django /home/django/app

# Switch to the non-root user
USER django

# Define the entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]