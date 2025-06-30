# ---- Stage 1: Build ----
# Use a slim Python image as a builder to keep the final image small
FROM python:3.10-slim-buster as builder

WORKDIR /usr/src/app

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Install system dependencies needed for some Python packages (e.g., psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev

# Copy the requirements file and install dependencies into a wheelhouse
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /usr/src/app/wheels -r requirements.txt


# ---- Stage 2: Final Image ----
# Use a fresh, slim Python image for the final product
FROM python:3.10-slim-buster

# Create a non-root user for security
RUN groupadd -r django && useradd -r -g django django

WORKDIR /home/django/app

# Install only runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev && rm -rf /var/lib/apt/lists/*

# Copy the pre-built wheels from the builder stage
COPY --from=builder /usr/src/app/wheels /wheels

# Install the dependencies from the local wheels
RUN pip install --no-cache /wheels/*

# Copy the entrypoint script and application code
COPY ./entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

COPY . .

# Change ownership to the non-root user
RUN chown -R django:django /home/django/app

# Switch to the non-root user
USER django

# Define the entrypoint for the container
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]