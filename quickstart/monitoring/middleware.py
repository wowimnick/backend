# monitoring/middleware.py

import time
from django.core.cache import cache
from collections import defaultdict
import threading
import traceback
import json

local = threading.local()

class MetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.requests = defaultdict(int)
        self.response_times = defaultdict(list)

    def __call__(self, request):
        # Store the overall start time 
        overall_start_time = time.time()
        
        # Store start time in thread-local storage
        local.start_time = overall_start_time
        
        # Track middleware start time
        middleware_start = time.time()
        
        # This will be captured once middleware execution is complete
        middleware_end = time.time()
        middleware_time = middleware_end - middleware_start
        
        # Store middleware time in request for later access
        request.middleware_time = middleware_time
        
        # Call the next middleware or view
        response = self.get_response(request)
        
        # Calculate view time (including template rendering)
        view_template_time = (time.time() - middleware_end)
        
        # Try to extract template rendering time if available
        template_time = getattr(response, '_template_render_time', 0)
        
        # Calculate view time (excluding template rendering)
        view_time = view_template_time - template_time
        
        # Store these values in response object for future reference
        response._middleware_time = middleware_time
        response._view_time = view_time
        response._template_time = template_time
        
        # Calculate total response time
        duration = time.time() - overall_start_time
        
        # Track metrics
        path = request.path.split('/')[1] or 'root'
        self.response_times[path].append(duration)
        self.requests[path] += 1

        # Store in cache for the metrics collector
        cache.set(f'response_time_{request.path}', duration, 3600)
        cache.set(f'middleware_time_{request.path}', middleware_time, 3600)
        cache.set(f'view_time_{request.path}', view_time, 3600)
        cache.set(f'template_time_{request.path}', template_time, 3600)
        
        cache.incr('requests_per_minute', 1)

        # Track errors
        if 400 <= response.status_code < 600:
            cache.incr('error_count', 1)
            cache.incr('error_rate', 1)
            
            # Track error by path
            cache.incr(f'error_count_{path}', 1)

        return response

    def process_exception(self, request, exception):
        path = request.path.split('/')[1] or 'root'
        
        # Increment error counters
        cache.incr('error_count', 1)
        cache.incr('error_rate', 1)
        cache.incr(f'error_count_{path}', 1)
        
        # Store exception details for debugging
        error_info = {
            'timestamp': time.time(),
            'path': request.path,
            'method': request.method,
            'exception_type': exception.__class__.__name__,
            'exception_message': str(exception),
            'traceback': traceback.format_exc()
        }
        
        # Store the last 20 errors
        errors = cache.get('recent_errors', [])
        errors.insert(0, error_info)
        cache.set('recent_errors', errors[:20], 3600)
        
        # Track exception types
        exception_type = exception.__class__.__name__
        exception_counts = cache.get('exception_counts', {})
        exception_counts[exception_type] = exception_counts.get(exception_type, 0) + 1
        cache.set('exception_counts', exception_counts, 3600)