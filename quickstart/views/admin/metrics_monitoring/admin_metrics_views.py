# quickstart/views/monitoring/admin_metrics_views.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework import status
from django.core.cache import cache
from django.conf import settings
from django.db import connection, transaction, OperationalError
from django.db.models import Count
from django.utils import timezone # Use timezone now
import psutil
import datetime
import time
import traceback
import logging
from collections import Counter

# Attempt to import Silk models safely
try:
    from silk.models import Request as SilkRequest, SQLQuery
    SILK_ENABLED = True
except ImportError:
    logging.warning("Django Silk not found or not configured. Profiling metrics will be unavailable.")
    SilkRequest = None
    SQLQuery = None
    SILK_ENABLED = False

logger = logging.getLogger(__name__)

# --- Permission Class ---
class CanViewSystemMetrics(BasePermission):
    """ Allows access only to users with 'view_system_metrics' permission. """
    message = "You do not have permission to view system metrics."
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated or not request.user.is_active:
             return False
        # Use the permission defined in models.py
        return request.user.has_perm('quickstart.view_system_metrics')

# --- Data Fetching Logic (Adapted from Consumer) ---

def get_silk_data():
    """ Synchronously fetches and processes Silk data. """
    if not SILK_ENABLED:
        return {
            'requests': [], 'avg_response_time': 0, 'avg_queries_per_request': 0,
            'error_analysis': {'top_error_paths': [], 'status_distribution': {}},
            'sql_analysis': {'slow_queries': [], 'query_types': {}}
        }
    try:
        # Fetch recent requests for detailed view (limit count)
        recent_requests_qs = SilkRequest.objects.select_related('response').order_by('-start_time')[:20] # Limit detail shown

        requests_data = []
        total_time_recent = 0
        num_queries_recent = 0

        for request in recent_requests_qs:
            time_taken = request.time_taken or 0
            total_time_recent += time_taken
            num_queries = request.num_sql_queries or 0
            num_queries_recent += num_queries

            status_code = 0 # Default status code if no response
            if hasattr(request, 'response') and request.response:
                try:
                    status_code = request.response.status_code
                except Exception as e:
                    logger.warning(f"Could not get status code for Silk Request {request.id}: {e}")

            db_time = request.time_spent_on_sql_queries or 0

            requests_data.append({
                'path': request.path,
                'method': request.method,
                'time_taken': round(time_taken, 2),
                'num_queries': num_queries,
                'time_db': round(db_time, 2),
                'time_view': round(max(0, time_taken - db_time), 2), # Simplified view time
                'status_code': status_code,
                'timestamp': request.start_time.strftime('%H:%M:%S') if request.start_time else 'N/A'
            })

        # Fetch last 100 for aggregate stats
        aggregate_requests_qs = SilkRequest.objects.select_related('response').order_by('-start_time')[:100]
        status_counts = Counter()
        error_paths = Counter()
        path_counts = Counter()
        total_requests_agg = 0

        for request in aggregate_requests_qs:
            total_requests_agg += 1
            path_key = request.path.split('/')[1] or 'root'
            path_counts[path_key] += 1

            status_code = 0 
            if hasattr(request, 'response') and request.response:
                try:
                    status_code = request.response.status_code
                except Exception as e:
                    logger.warning(f"Could not get status code for Silk Request {request.id} in aggregate: {e}")

            status_counts[status_code] += 1
            if status_code >= 400:
                error_paths[path_key] += 1

        # Calculate aggregates
        avg_response_time = round(total_time_recent / len(recent_requests_qs), 2) if recent_requests_qs else 0
        avg_queries_per_request = round(num_queries_recent / len(recent_requests_qs), 2) if recent_requests_qs else 0

        error_rates = {path: round((error_paths[path] / count) * 100, 1) for path, count in path_counts.items()}
        top_error_paths = sorted(error_rates.items(), key=lambda item: item[1], reverse=True)[:5]

        status_distribution = {
            "2xx": sum(count for status, count in status_counts.items() if 200 <= status < 300),
            "3xx": sum(count for status, count in status_counts.items() if 300 <= status < 400),
            "4xx": sum(count for status, count in status_counts.items() if 400 <= status < 500),
            "5xx": sum(count for status, count in status_counts.items() if 500 <= status < 600)
        }

        # SQL Analysis
        slow_queries_data = []
        query_types = Counter()
        try:
            # Use transaction.atomic for safety if needed, though reads should be fine
            with transaction.atomic():
                # Get top 10 slowest distinct queries (might need adjustment based on Silk version)
                # Using distinct('query') might not work directly with order_by('-time_taken') in all DBs
                # Fetch more and filter in Python if needed, or use window functions if DB supports
                sql_queries = SQLQuery.objects.order_by('-time_taken')[:20] # Fetch a bit more initially

                processed_queries = set()
                count = 0
                for query in sql_queries:
                     if count >= 10: break # Limit to 10 unique slow queries
                     if query.query in processed_queries: continue # Skip duplicates

                     query_text = query.query.strip().upper()
                     query_type = 'OTHER'
                     if query_text.startswith('SELECT'): query_type = 'SELECT'
                     elif query_text.startswith('INSERT'): query_type = 'INSERT'
                     elif query_text.startswith('UPDATE'): query_type = 'UPDATE'
                     elif query_text.startswith('DELETE'): query_type = 'DELETE'
                     query_types[query_type] += 1

                     slow_queries_data.append({
                         'query': query.query[:150] + ("..." if len(query.query) > 150 else ""),
                         'time_taken': round(query.time_taken, 2),
                         'query_type': query_type
                     })
                     processed_queries.add(query.query)
                     count += 1

        except Exception as sql_err:
             logger.error(f"Error fetching SQL query data from Silk: {sql_err}", exc_info=True)


        return {
            'requests': requests_data,
            'avg_response_time': avg_response_time,
            'avg_queries_per_request': avg_queries_per_request,
            'error_analysis': {
                'top_error_paths': top_error_paths,
                'status_distribution': status_distribution
            },
            'sql_analysis': {
                'slow_queries': slow_queries_data,
                'query_types': dict(query_types)
            }
        }
    except Exception as e:
        logger.error(f"Error getting Silk data: {e}", exc_info=True)
        # Return default structure on error
        return {
            'requests': [], 'avg_response_time': 0, 'avg_queries_per_request': 0,
            'error_analysis': {'top_error_paths': [], 'status_distribution': {}},
            'sql_analysis': {'slow_queries': [], 'query_types': {}}
        }

def get_endpoint_request_counts(time_window_hours=1):
    """ Fetches and aggregates request counts per endpoint view name from Silk. """
    cache_key = f'monitoring_endpoint_counts_{time_window_hours}h'
    cache_ttl = 60 # seconds cache
    cached_data = cache.get(cache_key)

    if cached_data:
        # Optional: Add timestamp check if needed for more fine-grained caching
        # logger.debug("Returning cached endpoint counts")
        return cached_data

    if not SILK_ENABLED or not SilkRequest:
        return {'top_endpoints': [], 'total_requests': 0}

    logger.debug(f"Fetching fresh endpoint counts (last {time_window_hours}h)")
    try:
        start_time_filter = timezone.now() - datetime.timedelta(hours=time_window_hours)

        # Aggregate requests by view_name within the time window
        endpoint_counts = SilkRequest.objects.filter(
            start_time__gte=start_time_filter,
            view_name__isnull=False # Exclude requests without a view name
        ).values(
            'view_name' # Group by the resolved view name
        ).annotate(
            request_count=Count('id')
        ).order_by(
            '-request_count' # Order by most frequent
        )[:20] # Limit to top N endpoints

        total_requests_in_window = SilkRequest.objects.filter(start_time__gte=start_time_filter).count()

        result = {
            'top_endpoints': list(endpoint_counts), # Convert queryset to list
            'total_requests': total_requests_in_window,
            'time_window_hours': time_window_hours
        }

        cache.set(cache_key, result, cache_ttl)
        return result

    except Exception as e:
        logger.error(f"Error getting endpoint request counts from Silk: {e}", exc_info=True)
        # Return default structure on error, potentially return old cache if available
        return cached_data or {'top_endpoints': [], 'total_requests': 0, 'time_window_hours': time_window_hours}
    
def get_db_stats():
    """ Synchronously fetches basic DB stats (PostgreSQL specific). """
    # Check cache first
    cached_data = cache.get('monitoring_db_metrics')
    cache_ttl = 10 # seconds
    if cached_data:
        # Check timestamp if stored with data
        timestamp = cached_data.get('timestamp', 0)
        if time.time() - timestamp < cache_ttl:
             # logger.debug("Returning cached DB metrics")
             return cached_data['data']

    logger.debug("Fetching fresh DB metrics")
    db_stats = { 'active_connections': 0, 'slow_queries': 0, 'connection_pool': "N/A", 'pool_usage_percent': 0 }
    try:
        with connection.cursor() as cursor:
            # Active Connections
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active';")
            result = cursor.fetchone()
            db_stats['active_connections'] = result[0] if result else 0

            # Slow Queries (example: > 1 second) - might need tuning
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND now() - query_start > interval '1 second';")
            result = cursor.fetchone()
            db_stats['slow_queries'] = result[0] if result else 0

            # Max Connections
            cursor.execute("SHOW max_connections;")
            result = cursor.fetchone()
            max_connections = int(result[0]) if result else 0

            if max_connections > 0:
                 db_stats['connection_pool'] = f"{db_stats['active_connections']}/{max_connections}"
                 db_stats['pool_usage_percent'] = round((db_stats['active_connections'] / max_connections) * 100, 1)
            else:
                 db_stats['connection_pool'] = f"{db_stats['active_connections']}/?"


        # Store in cache with timestamp
        cache.set('monitoring_db_metrics', {'timestamp': time.time(), 'data': db_stats}, cache_ttl + 5) # Cache slightly longer
        return db_stats

    except OperationalError as db_err:
         logger.error(f"Database operational error getting stats: {db_err}")
         # Return last known good cache if available? Or defaults.
         return cached_data['data'] if cached_data else db_stats # Return previous cache or defaults
    except Exception as e:
        logger.error(f"Error getting DB stats: {e}", exc_info=True)
        return cached_data['data'] if cached_data else db_stats # Return previous cache or defaults


# --- API View ---

class AdminMetricsView(APIView):
    """
    API Endpoint to provide system and application metrics for the admin dashboard.
    Requires 'view_system_metrics' permission.
    """
    permission_classes = [IsAuthenticated, CanViewSystemMetrics]

    # Rate limiting can be added here if needed
    # throttle_classes = [...]

    # Store previous network stats for rate calculation
    prev_network_stats = None
    last_network_time = 0

    def get(self, request, *args, **kwargs):
        """ Returns the latest metrics data. """
        start_time = time.time()
        metrics = {}

        try:
            # 1. System Metrics (psutil - fetch every time, usually fast)
            cpu_percent = psutil.cpu_percent(interval=0.1)
            cpu_times = psutil.cpu_times_percent()
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            net_io = psutil.net_io_counters()
            current_time = time.time()

            # Calculate network rates
            net_rates = {'bytes_sent_rate': 0, 'bytes_recv_rate': 0}
            time_diff = current_time - self.__class__.last_network_time
            if self.__class__.prev_network_stats and time_diff > 0:
                net_rates['bytes_sent_rate'] = round((net_io.bytes_sent - self.__class__.prev_network_stats.bytes_sent) / time_diff)
                net_rates['bytes_recv_rate'] = round((net_io.bytes_recv - self.__class__.prev_network_stats.bytes_recv) / time_diff)

            self.__class__.prev_network_stats = net_io
            self.__class__.last_network_time = current_time

            metrics["system"] = {
                "cpu_usage": cpu_percent,
                "cpu_detailed": {"user": cpu_times.user, "system": cpu_times.system, "idle": cpu_times.idle},
                "memory": {"total": mem.total, "available": mem.available, "percent": mem.percent, "used": mem.used},
                "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "percent": disk.percent},
                "network": {**net_rates, "packets_sent": net_io.packets_sent, "packets_recv": net_io.packets_recv} # Include latest packet counts
            }

            # 2. Database Metrics (Use cached function)
            metrics["database"] = get_db_stats()

            # 3. Application Metrics (Read from cache - populated by middleware/tasks)
            # Ensure these keys exist in cache with defaults
            cache.add('active_users', 0, timeout=300) # Set if not exists
            cache.add('requests_per_minute', 0, timeout=70)
            cache.add('error_rate', 0, timeout=70) # Should represent errors per interval
            cache.add('start_time', timezone.now(), timeout=None) # Persist start time

            metrics["application"] = {
                "active_users": cache.get('active_users', 0), # How is this updated? Needs separate logic.
                "error_rate": cache.get('error_rate', 0), # How is this calculated/reset?
                "requests_per_minute": cache.get('requests_per_minute', 0), # How is this reset?
                # Uptime calculation
                "uptime_seconds": (timezone.now() - cache.get('start_time')).total_seconds()
            }

            # 4. Cache Metrics (Read from cache - populated elsewhere?)
            cache.add('cache_hits', 0)
            cache.add('cache_misses', 0)
            metrics["cache"] = {
                "hits": cache.get('cache_hits', 0),
                "misses": cache.get('cache_misses', 0),
                # Add other cache stats if available from backend (e.g., memory usage)
            }

            # 5. Profiling Metrics (Use cached function)
            metrics["profiling"] = get_silk_data() # Fetches if needed, uses cache
            metrics["endpoint_analysis"] = get_endpoint_request_counts(time_window_hours=1) # Get counts for the last hour
            # Add overall response time for this API call
            metrics["api_response_time_ms"] = round((time.time() - start_time) * 1000, 2)

            return Response(metrics)

        except Exception as e:
            logger.error(f"Error retrieving metrics data: {e}", exc_info=True)
            return Response({"error": "Failed to retrieve metrics data."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)