# monitoring/middleware.py

import time
from django.core.cache import cache
from collections import defaultdict
import threading

local = threading.local()

class MetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.requests = defaultdict(int)
        self.response_times = defaultdict(list)

    def __call__(self, request):
        start_time = time.time()
        
        # Store start time in thread-local storage
        local.start_time = start_time

        response = self.get_response(request)

        # Calculate response time
        duration = time.time() - start_time
        
        # Track metrics
        path = request.path.split('/')[1] or 'root'
        self.response_times[path].append(duration)
        self.requests[path] += 1

        # Store in cache for the metrics collector
        cache.set(f'response_time_{request.path}', duration, 3600)
        cache.incr('requests_per_minute', 1)

        # Track errors
        if 400 <= response.status_code < 600:
            cache.incr('error_count', 1)

        return response

    def process_exception(self, request, exception):
        cache.incr('error_count', 1)
        cache.incr('error_rate', 1)