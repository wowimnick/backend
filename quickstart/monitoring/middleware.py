# quickstart/monitoring/middleware.py

import time
from django.core.cache import cache
import traceback
import logging # Added logging

logger = logging.getLogger(__name__)

# Removed defaultdict, threading as they are no longer used here

class MetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        # Initialize cache keys if they don't exist, with a reasonable timeout
        # This helps prevent NoneType errors if cache expires or restarts
        cache.add('requests_per_minute', 0, timeout=70) # Set if not exists, expire slightly > 1 min
        cache.add('error_count', 0, timeout=None) # Persist error count unless manually reset
        cache.add('error_rate', 0, timeout=70) # Track errors per interval, expire slightly > 1 min

    def __call__(self, request):
        start_time = time.time()

        response = None # Initialize response
        try:
            response = self.get_response(request)
        except Exception as e:
            raise e

        duration = time.time() - start_time

        # --- Track Aggregate Metrics ---

        # Requests per minute counter (reset periodically by a separate task or metric system)
        try:
            # Use incr, handles initialization if key expired
            cache.incr('requests_per_minute')
        except Exception as cache_err:
             logger.error(f"Cache error incrementing requests_per_minute: {cache_err}")


        # Track errors based on response status code
        if response and 400 <= response.status_code < 600:
             self.handle_error_metrics(request, response, is_exception=False)

        return response

    def process_exception(self, request, exception):
        """
        Called when an exception occurs during view processing.
        Logs error metrics and exception details.
        """
        # Log error metrics
        self.handle_error_metrics(request, None, is_exception=True, exception=exception)

        # Returning None lets Django's default exception handling continue
        return None

    def handle_error_metrics(self, request, response=None, is_exception=False, exception=None):
        """ Helper function to increment error counters and log details. """
        path_prefix = request.path.split('/')[1] or 'root' # Get first part of path

        try:
            # Increment general error counters
            cache.incr('error_count')
            cache.incr('error_rate') # Represents errors in the current interval

            # Increment error counter for the specific path prefix
            cache.incr(f'error_count_{path_prefix}')
        except Exception as cache_err:
             logger.error(f"Cache error incrementing error metrics: {cache_err}")


        # Log exception details if it's an exception
        if is_exception and exception:
             try:
                 error_info = {
                     'timestamp': time.time(),
                     'path': request.path,
                     'method': request.method,
                     'exception_type': exception.__class__.__name__,
                     'exception_message': str(exception),
                     'traceback': traceback.format_exc() # Full traceback
                 }

                 # Store the last N errors (e.g., 20)
                 errors = cache.get('recent_errors', [])
                 errors.insert(0, error_info) # Add to beginning
                 # Use cache 'set' with timeout if errors should expire
                 cache.set('recent_errors', errors[:20], timeout=3600) # Store for 1 hour

                 # Track exception types counts
                 exception_type = exception.__class__.__name__
                 exception_counts = cache.get('exception_counts', {})
                 exception_counts[exception_type] = exception_counts.get(exception_type, 0) + 1
                 cache.set('exception_counts', exception_counts, timeout=3600) # Store for 1 hour
             except Exception as log_err:
                  logger.error(f"Error logging exception details to cache: {log_err}")