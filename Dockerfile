# Use a specific, stable Python version
FROM python:3.10-slim-buster

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=off
ENV PIP_DISABLE_PIP_VERSION_CHECK=on

# Set the working directory
WORKDIR /home/django/app

# Install system dependencies required by Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    jq \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file
COPY requirements.txt .

# Install Python dependencies
# This creates a cached layer. It will only re-run if requirements.txt changes.
RUN pip install -r requirements.txt

# Create a non-root user for security
RUN groupadd -r django && useradd -r -g django django

# Copy the rest of the application code
COPY . .

# Copy the entrypoint script and make it executable
COPY ./entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Change ownership of the app directory to the non-root user
RUN chown -R django:django /home/django/app

# Switch to the non-root user
USER django

# Define the entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]