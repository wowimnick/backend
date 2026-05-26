# quickstart/views/monitoring/admin_metrics_views.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.cache import cache
from django.conf import settings
from django.db import connection, transaction, OperationalError
from django.db.models import Count, Avg
from django.utils import timezone
import psutil
import datetime
import time
import traceback
import logging
from collections import Counter
import redis

# Attempt to import Celery app safely
try:
    from CEBackend.celery import app as celery_app

    CELERY_ENABLED = True
except ImportError:
    logging.warning("Celery app not found. Celery metrics will be unavailable.")
    celery_app = None
    CELERY_ENABLED = False

# Silk profiling was removed; metrics use a stub when profiling is disabled.
SILK_ENABLED = False
SilkRequest = None
SQLQuery = None

from quickstart.utils.permissions import IsAuthenticated, BasePermission, CanViewSystemMetrics

from quickstart.utils.beat_schedule_info import get_beat_schedule_tasks

logger = logging.getLogger(__name__)


# --- Data Fetching Logic ---


def get_celery_stats():
    """Fetches and aggregates Celery worker and queue stats."""
    if not CELERY_ENABLED:
        return {
            "active_workers": 0,
            "queue_length": 0,
            "failed_tasks_count": 0,
            "workers": [],
            "failed_tasks": [],
            "scheduled_tasks": get_beat_schedule_tasks(),
            "server_time": timezone.now().isoformat(),
        }

    try:
        inspector = celery_app.control.inspect(timeout=1)

        active_workers_info = inspector.ping()
        workers_data = (
            [{"name": name, "status": "online"} for name in active_workers_info.keys()]
            if active_workers_info
            else []
        )
        active_workers_count = len(workers_data)

        queue_length = 0
        try:
            queue_name = getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "celery")
            redis_url = getattr(
                settings, "CELERY_BROKER_URL", "redis://localhost:6379/1"
            )
            r = redis.from_url(redis_url)
            queue_length = r.llen(queue_name)
        except Exception as redis_err:
            logger.error(f"Could not connect to Redis to get queue length: {redis_err}")
            active_queues = inspector.active_queues()
            if active_queues:
                queue_length = sum(
                    len(tasks)
                    for worker_tasks in active_queues.values()
                    for tasks in worker_tasks
                )

        # Get failed tasks from cache
        failed_tasks = cache.get("celery_failed_tasks", [])

        scheduled_tasks = get_beat_schedule_tasks()

        return {
            "active_workers": active_workers_count,
            "queue_length": queue_length,
            "failed_tasks_count": len(failed_tasks),
            "workers": workers_data,
            "failed_tasks": failed_tasks,  # Return the full list of failed tasks
            "scheduled_tasks": scheduled_tasks,
            "server_time": timezone.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"Error getting Celery stats: {e}", exc_info=True)
        return {
            "active_workers": 0,
            "queue_length": 0,
            "failed_tasks_count": 0,
            "workers": [],
            "failed_tasks": [],
            "scheduled_tasks": get_beat_schedule_tasks(),
            "server_time": timezone.now().isoformat(),
        }

def get_silk_data():
    """ 
    Synchronously fetches and processes Silk data and recent errors from cache.
    This function is now wrapped with a caching layer to improve performance.
    """
    # --- Caching Layer ---
    cache_key = 'monitoring_silk_and_errors_data'
    cache_ttl = 10 # Cache for 10 seconds, slightly longer than the frontend poll interval
    cached_data = cache.get(cache_key)
    if cached_data:
        # logger.debug("Returning cached Silk & Error data")
        return cached_data
    # --- End Caching Layer ---

    logger.debug("Fetching fresh Silk & Error data")
    
    # Fetch recent errors from cache (this is fast)
    recent_errors = cache.get('recent_errors', [])
    
    # The main logic is now inside a try...except block to handle potential Silk errors
    try:
        if not SILK_ENABLED:
            result = {
                'requests': [], 
                'avg_response_time': 0, 
                'avg_queries_per_request': 0,
                'error_analysis': {'top_error_paths': [], 'status_distribution': {}},
                'sql_analysis': {'slow_queries': [], 'query_types': {}},
                'recent_errors': recent_errors
            }
            cache.set(cache_key, result, cache_ttl) # Cache the "disabled" state
            return result

        # --- Silk Data Fetching Logic (from original implementation) ---
        
        # Fetch recent requests for detailed view (limit count)
        recent_requests_qs = SilkRequest.objects.select_related('response').order_by('-start_time')[:20]

        requests_data = []
        total_time_recent = 0
        num_queries_recent = 0

        for request in recent_requests_qs:
            time_taken = request.time_taken or 0
            total_time_recent += time_taken
            num_queries = request.num_sql_queries or 0
            num_queries_recent += num_queries

            status_code = 0
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
                'time_view': round(max(0, time_taken - db_time), 2),
                'status_code': status_code,
                'timestamp': request.start_time.strftime('%H:%M:%S') if request.start_time else 'N/A'
            })

        # Fetch last 100 for aggregate stats
        aggregate_requests_qs = SilkRequest.objects.select_related('response').order_by('-start_time')[:100]
        status_counts = Counter()
        error_paths = Counter()
        path_counts = Counter()
        
        for request in aggregate_requests_qs:
            path_key = request.path.split('/')[1] or 'root'
            path_counts[path_key] += 1
            status_code = 0 
            if hasattr(request, 'response') and request.response:
                status_code = request.response.status_code
            status_counts[status_code] += 1
            if status_code >= 400:
                error_paths[path_key] += 1
        
        # Calculate aggregates
        avg_response_time = round(total_time_recent / len(recent_requests_qs), 2) if recent_requests_qs else 0
        avg_queries_per_request = round(num_queries_recent / len(recent_requests_qs), 2) if recent_requests_qs else 0
        error_rates = {path: round((error_paths.get(path, 0) / count) * 100, 1) for path, count in path_counts.items()}
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
            with transaction.atomic():
                sql_queries = SQLQuery.objects.order_by('-time_taken')[:20]
                processed_queries = set()
                count = 0
                for query in sql_queries:
                    if count >= 10: break
                    if query.query in processed_queries: continue
                    query_text = query.query.strip().upper()
                    query_type = 'OTHER'
                    if query_text.startswith('SELECT'): query_type = 'SELECT'
                    elif query_text.startswith('INSERT'): query_type = 'INSERT'
                    elif query_text.startswith('UPDATE'): query_type = 'UPDATE'
                    elif query_text.startswith('DELETE'): query_type = 'DELETE'
                    query_types[query_type] += 1
                    slow_queries_data.append({ 'query': query.query[:150] + "...", 'time_taken': round(query.time_taken, 2), 'query_type': query_type})
                    processed_queries.add(query.query)
                    count += 1
        except Exception as sql_err:
            logger.error(f"Error fetching SQL query data from Silk: {sql_err}", exc_info=True)
        
        # --- Final result construction and caching ---
        final_result = {
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
            },
            'recent_errors': recent_errors
        }
        
        cache.set(cache_key, final_result, cache_ttl) # Set cache on success
        return final_result

    except Exception as e:
        logger.error(f"Critical error in get_silk_data: {e}", exc_info=True)
        # On error, return a default structure but do NOT cache the error state,
        # so the next poll can try again.
        return {
            'requests': [], 
            'avg_response_time': 0, 
            'avg_queries_per_request': 0,
            'error_analysis': {'top_error_paths': [], 'status_distribution': {}},
            'sql_analysis': {'slow_queries': [], 'query_types': {}},
            'recent_errors': recent_errors # Return any errors we fetched before the crash
        }


def get_endpoint_analysis(time_window_hours=1):
    """Fetches and aggregates request counts and performance per endpoint view name from Silk."""
    cache_key = f"monitoring_endpoint_analysis_{time_window_hours}h"
    cache_ttl = 60
    cached_data = cache.get(cache_key)

    if cached_data:
        return cached_data

    if not SILK_ENABLED or not SilkRequest:
        return {
            "top_by_count": [],
            "top_by_time": [],
            "top_by_queries": [],
            "total_requests": 0,
            "time_window_hours": time_window_hours,
        }

    logger.debug(f"Fetching fresh endpoint analysis (last {time_window_hours}h)")
    try:
        start_time_filter = timezone.now() - datetime.timedelta(hours=time_window_hours)

        base_query = (
            SilkRequest.objects.filter(
                start_time__gte=start_time_filter, view_name__isnull=False
            )
            .values("view_name")
            .annotate(
                request_count=Count("id"),
                avg_time_taken=Avg("time_taken"),
                avg_db_queries=Avg("num_sql_queries"),
            )
        )

        top_by_count = list(base_query.order_by("-request_count")[:10])
        top_by_time = list(base_query.order_by("-avg_time_taken")[:10])
        top_by_queries = list(base_query.order_by("-avg_db_queries")[:10])

        total_requests_in_window = SilkRequest.objects.filter(
            start_time__gte=start_time_filter
        ).count()

        result = {
            "top_by_count": top_by_count,
            "top_by_time": top_by_time,
            "top_by_queries": top_by_queries,
            "total_requests": total_requests_in_window,
            "time_window_hours": time_window_hours,
        }

        cache.set(cache_key, result, cache_ttl)
        return result

    except Exception as e:
        logger.error(f"Error getting endpoint analysis from Silk: {e}", exc_info=True)
        return cached_data or {
            "top_by_count": [],
            "top_by_time": [],
            "top_by_queries": [],
            "total_requests": 0,
            "time_window_hours": time_window_hours,
        }


def get_db_stats():
    """Synchronously fetches basic DB stats (PostgreSQL specific)."""
    cached_data = cache.get("monitoring_db_metrics")
    cache_ttl = 10
    if cached_data:
        if time.time() - cached_data.get("timestamp", 0) < cache_ttl:
            return cached_data["data"]

    logger.debug("Fetching fresh DB metrics")
    db_stats = {
        "active_connections": 0,
        "slow_queries": 0,
        "connection_pool": "N/A",
        "pool_usage_percent": 0,
    }
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE state = 'active';"
            )
            result = cursor.fetchone()
            db_stats["active_connections"] = result[0] if result else 0

            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND now() - query_start > interval '1 second';"
            )
            result = cursor.fetchone()
            db_stats["slow_queries"] = result[0] if result else 0

            cursor.execute("SHOW max_connections;")
            result = cursor.fetchone()
            max_connections = int(result[0]) if result else 0

            if max_connections > 0:
                db_stats["connection_pool"] = (
                    f"{db_stats['active_connections']}/{max_connections}"
                )
                db_stats["pool_usage_percent"] = round(
                    (db_stats["active_connections"] / max_connections) * 100, 1
                )
            else:
                db_stats["connection_pool"] = f"{db_stats['active_connections']}/?"

        cache.set(
            "monitoring_db_metrics",
            {"timestamp": time.time(), "data": db_stats},
            cache_ttl + 5,
        )
        return db_stats
    except Exception as e:
        logger.error(f"Error getting DB stats: {e}", exc_info=True)
        return cached_data["data"] if cached_data else db_stats


# --- API View ---


class AdminMetricsView(APIView):
    """
    API Endpoint to provide system and application metrics for the admin dashboard.
    Requires 'view_system_metrics' permission.
    """

    permission_classes = [IsAuthenticated, CanViewSystemMetrics]
    prev_network_stats = None
    last_network_time = 0

    def get(self, request, *args, **kwargs):
        """Returns the latest metrics data."""
        start_time = time.time()
        metrics = {}

        try:
            # 1. System Metrics
            cpu_percent = psutil.cpu_percent(interval=0.1)
            cpu_times = psutil.cpu_times_percent()
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            net_io = psutil.net_io_counters()
            current_time = time.time()
            net_rates = {"bytes_sent_rate": 0, "bytes_recv_rate": 0}
            time_diff = current_time - self.__class__.last_network_time
            if self.__class__.prev_network_stats and time_diff > 0:
                net_rates["bytes_sent_rate"] = round(
                    (net_io.bytes_sent - self.__class__.prev_network_stats.bytes_sent)
                    / time_diff
                )
                net_rates["bytes_recv_rate"] = round(
                    (net_io.bytes_recv - self.__class__.prev_network_stats.bytes_recv)
                    / time_diff
                )
            self.__class__.prev_network_stats = net_io
            self.__class__.last_network_time = current_time

            metrics["system"] = {
                "cpu_usage": cpu_percent,
                "cpu_detailed": {
                    "user": cpu_times.user,
                    "system": cpu_times.system,
                    "idle": cpu_times.idle,
                },
                "memory": {
                    "total": mem.total,
                    "available": mem.available,
                    "percent": mem.percent,
                    "used": mem.used,
                },
                "disk": {
                    "total": disk.total,
                    "used": disk.used,
                    "free": disk.free,
                    "percent": disk.percent,
                },
                "network": {
                    **net_rates,
                    "packets_sent": net_io.packets_sent,
                    "packets_recv": net_io.packets_recv,
                },
            }

            # 2. Database Metrics
            metrics["database"] = get_db_stats()

            # 3. Celery / Background Task Metrics
            metrics["celery"] = get_celery_stats()

            # 4. Application Metrics
            cache.add("active_users", 0, timeout=300)
            cache.add("requests_per_minute", 0, timeout=70)
            cache.add("error_rate", 0, timeout=70)
            cache.add("start_time", timezone.now(), timeout=None)
            metrics["application"] = {
                "active_users": cache.get("active_users", 0),
                "error_rate": cache.get("error_rate", 0),
                "requests_per_minute": cache.get("requests_per_minute", 0),
                "uptime_seconds": (
                    timezone.now() - cache.get("start_time")
                ).total_seconds(),
            }

            # 5. Cache Metrics
            cache.add("cache_hits", 0)
            cache.add("cache_misses", 0)
            metrics["cache"] = {
                "hits": cache.get("cache_hits", 0),
                "misses": cache.get("cache_misses", 0),
            }

            # 6. Profiling Metrics
            metrics["profiling"] = get_silk_data()
            metrics["endpoint_analysis"] = get_endpoint_analysis(time_window_hours=1)

            metrics["api_response_time_ms"] = round(
                (time.time() - start_time) * 1000, 2
            )

            return Response(metrics)

        except Exception as e:
            logger.error(f"Error retrieving metrics data: {e}", exc_info=True)
            return Response(
                {"error": "Failed to retrieve metrics data."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
