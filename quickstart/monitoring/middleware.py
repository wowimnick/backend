# quickstart/monitoring/middleware.py (CORRECTED VERSION)

import time
import traceback
import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)


class MetricsMiddleware:
    def __init__(self, get_response):
        """
        Initialization of middleware.
        IMPORTANT: DO NOT access the cache or database in __init__.
        This method is called once per worker at startup. Any dependency on
        external services here will make the application fragile and can
        prevent workers from starting.
        """
        self.get_response = get_response

    def __call__(self, request):
        """
        This method is called for every request. It's the safe place
        to interact with the cache.
        """
        start_time = time.time()

        # The `process_exception` hook will handle unhandled exceptions.
        response = self.get_response(request)

        # We place this here to ensure it runs *after* any potential exception
        # has been handled by process_exception.
        duration = time.time() - start_time

        # --- Safely increment request counter on every call ---
        try:
            try:
                cache.incr("requests_per_minute")
            except ValueError:
                cache.add("requests_per_minute", 1, timeout=70)
        except Exception as cache_err:
            logger.error(f"Cache error incrementing requests_per_minute: {cache_err}")

        # Track "handled" errors (e.g., 404 Not Found, 403 Forbidden)
        if response and 400 <= response.status_code < 600:
            self.handle_error_metrics(request, response)

        return response

    def process_exception(self, request, exception):
        """
        Called by Django's handler for unhandled exceptions. This is the
        correct place to hook into application errors.
        """
        self.handle_error_metrics(request, is_exception=True, exception=exception)
        # Return None to allow Django's default exception processing to continue.
        return None

    def handle_error_metrics(
        self, request, response=None, is_exception=False, exception=None
    ):
        """
        Helper function to increment error counters and log details.
        This is now safe because it's only called during a request.
        """
        try:
            for key, timeout in (("error_count", None), ("error_rate", 70)):
                try:
                    cache.incr(key)
                except ValueError:
                    cache.add(key, 1, timeout=timeout)

            # Log detailed exception info if available.
            if is_exception and exception:
                self.log_exception_details_to_cache(request, exception)

        except Exception as cache_err:
            logger.error(f"Cache error in handle_error_metrics: {cache_err}")

    def log_exception_details_to_cache(self, request, exception):
        """Logs detailed exception info into a capped list in the cache."""
        try:
            error_info = {
                "timestamp": time.time(),
                "path": request.path,
                "method": request.method,
                "exception_type": exception.__class__.__name__,
                "exception_message": str(exception),
                "traceback": traceback.format_exc(),
            }

            # Get existing list or create a new one.
            errors = cache.get("recent_errors", [])
            errors.insert(0, error_info)  # Add new error to the top.

            # Store the list, capped at 20 recent errors, for 1 hour.
            cache.set("recent_errors", errors[:20], timeout=3600)

        except Exception as log_err:
            logger.error(f"Error logging exception details to cache: {log_err}")
