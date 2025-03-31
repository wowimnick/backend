# monitoring/consumers.py
from channels.generic.websocket import AsyncWebsocketConsumer
import json
import asyncio
import psutil
import datetime
import time
from django.core.cache import cache
from silk.profiling.profiler import silk_profile
from asgiref.sync import sync_to_async
from silk.models import Request as SilkRequest, SQLQuery
from django.db import connection, transaction
import traceback
from collections import Counter

class MetricsConsumer(AsyncWebsocketConsumer):
    # Class variable for shared caching
    _cached_db_metrics = None
    _cached_db_metrics_timestamp = 0
    _cached_silk_data = None
    _cached_silk_data_timestamp = 0
    
    # Cache intervals (in seconds)
    DB_METRICS_CACHE_INTERVAL = 10  # Refresh database metrics every 10 seconds
    SILK_DATA_CACHE_INTERVAL = 15   # Refresh request profiling data every 15 seconds
    SYSTEM_METRICS_INTERVAL = 2     # Refresh system metrics every 2 seconds

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prev_network = None
        self.send_task = None
        self.is_connected = False
        self.db_refresh_task = None
        self.silk_refresh_task = None
        self.client_id = None
        # Track what data has been sent
        self.last_sent_metrics = None

    async def connect(self):
        """Handle initial connection"""
        print("Client attempting to connect")
        await self.accept()
        self.is_connected = True
        self.client_id = id(self)  # Unique ID for this client
        print(f"Client connected successfully, ID: {self.client_id}")
        
        # Send initial full data payload
        await self.send_initial_data()
        
        # Create background tasks to refresh cached data
        self.db_refresh_task = asyncio.create_task(self.refresh_db_metrics_loop())
        self.silk_refresh_task = asyncio.create_task(self.refresh_silk_data_loop())
        self.send_task = asyncio.create_task(self.send_metrics())

    async def send_initial_data(self):
        """Send the initial full data payload to the client"""
        # Get current metrics
        metrics = await self.get_full_metrics()
        self.last_sent_metrics = metrics
        
        # Send with special initialData flag
        await self.send(text_data=json.dumps({
            "initialData": True,
            "data": metrics
        }))

    async def disconnect(self, close_code):
        """Handle disconnection"""
        self.is_connected = False
        print(f"Client disconnected with code {close_code}, ID: {self.client_id}")
        
        # Cancel all background tasks
        for task in [self.send_task, self.db_refresh_task, self.silk_refresh_task]:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def receive(self, text_data):
        """Handle received messages"""
        try:
            message = json.loads(text_data)
            
            # Handle request for historical data
            if message.get('type') == 'request_historical':
                await self.send_historical_data(message.get('timespan', 300))
            
            # Handle change in update frequency
            elif message.get('type') == 'set_frequency':
                new_frequency = message.get('frequency', self.SYSTEM_METRICS_INTERVAL)
                # Validate frequency to prevent abuse
                if 1 <= new_frequency <= 30:
                    self.SYSTEM_METRICS_INTERVAL = new_frequency
        except json.JSONDecodeError:
            pass

    async def send_historical_data(self, timespan):
        """Send historical data for a specific timespan"""
        # Implement this method to retrieve and send historical data
        # This would fetch from your database or cache based on the requested timespan
        pass

    async def get_full_metrics(self):
        """Get the complete metrics data"""
        # Get system metrics
        cpu_times = psutil.cpu_times_percent()
        disk = psutil.disk_usage('/')
        network = psutil.net_io_counters()
        current_time = time.time()
        
        # Calculate network rates
        if self.prev_network is None:
            network_rates = {
                'bytes_sent': 0,
                'bytes_recv': 0,
                'packets_sent': network.packets_sent,
                'packets_recv': network.packets_recv
            }
        else:
            time_diff = current_time - self.prev_network['timestamp']
            if time_diff > 0:  # Prevent division by zero
                network_rates = {
                    'bytes_sent': round((network.bytes_sent - self.prev_network['bytes_sent']) / time_diff),
                    'bytes_recv': round((network.bytes_recv - self.prev_network['bytes_recv']) / time_diff),
                    'packets_sent': network.packets_sent,
                    'packets_recv': network.packets_recv
                }
            else:
                network_rates = {
                    'bytes_sent': 0,
                    'bytes_recv': 0,
                    'packets_sent': network.packets_sent,
                    'packets_recv': network.packets_recv
                }

        self.prev_network = {
            'bytes_sent': network.bytes_sent,
            'bytes_recv': network.bytes_recv,
            'timestamp': current_time
        }
        
        # Use cached database metrics
        db_metrics = self.__class__._cached_db_metrics
        if db_metrics is None:
            # If not cached yet, try to get from Redis
            db_metrics = cache.get('monitoring_db_metrics', {
                'active_connections': 0,
                'slow_queries': 0,
                'connection_pool': "0/0",
                'pool_usage_percent': 0
            })
            # And update class cache
            self.__class__._cached_db_metrics = db_metrics
            self.__class__._cached_db_metrics_timestamp = current_time
        
        # Use cached Silk data
        silk_data = self.__class__._cached_silk_data
        if silk_data is None:
            # If not cached yet, try to get from Redis
            silk_data = cache.get('monitoring_silk_data', {
                'requests': [],
                'avg_response_time': 0,
                'avg_queries_per_request': 0,
                'error_analysis': {'top_error_paths': [], 'status_distribution': {}},
                'sql_analysis': {'slow_queries': [], 'query_types': {}}
            })
            # And update class cache
            self.__class__._cached_silk_data = silk_data
            self.__class__._cached_silk_data_timestamp = current_time
        
        # Compile all metrics
        return {
            "system": {
                "cpu_usage": psutil.cpu_percent(),
                "cpu_detailed": {
                    "user": cpu_times.user,
                    "system": cpu_times.system,
                    "idle": cpu_times.idle
                },
                "memory": {
                    "total": psutil.virtual_memory().total,
                    "available": psutil.virtual_memory().available,
                    "percent": psutil.virtual_memory().percent,
                    "used": psutil.virtual_memory().used
                },
                "disk": {
                    "total": disk.total,
                    "used": disk.used,
                    "free": disk.free,
                    "percent": disk.percent
                },
                "network": network_rates
            },
            "database": db_metrics,
            "application": {
                "active_users": cache.get('active_users', 0),
                "error_rate": cache.get('error_rate', 0),
                "requests_per_minute": cache.get('requests_per_minute', 0),
                "uptime": str(datetime.datetime.now() - cache.get('start_time', datetime.datetime.now()))
            },
            "cache": {
                "hits": cache.get('cache_hits', 0),
                "misses": cache.get('cache_misses', 0)
            },
            "profiling": silk_data
        }

    @classmethod
    async def refresh_db_metrics_loop(cls):
        """Background task to refresh database metrics at a controlled interval"""
        try:
            while True:
                try:
                    # Check if we need to refresh
                    current_time = time.time()
                    if (current_time - cls._cached_db_metrics_timestamp) >= cls.DB_METRICS_CACHE_INTERVAL:
                        # Get fresh data
                        db_metrics = await cls.get_db_stats_sync()
                        
                        # Update cache
                        cls._cached_db_metrics = db_metrics
                        cls._cached_db_metrics_timestamp = current_time
                        
                        # Also store in Redis for persistence across instances
                        cache.set('monitoring_db_metrics', db_metrics, cls.DB_METRICS_CACHE_INTERVAL * 2)
                    
                    await asyncio.sleep(cls.DB_METRICS_CACHE_INTERVAL)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"Error in DB metrics refresh loop: {e}")
                    await asyncio.sleep(5)  # Wait longer on error
        except asyncio.CancelledError:
            print("DB metrics refresh task cancelled")

    @classmethod
    async def refresh_silk_data_loop(cls):
        """Background task to refresh Silk profiling data at a controlled interval"""
        try:
            while True:
                try:
                    # Check if we need to refresh
                    current_time = time.time()
                    if (current_time - cls._cached_silk_data_timestamp) >= cls.SILK_DATA_CACHE_INTERVAL:
                        # Get fresh data
                        silk_data = await cls.get_silk_data_sync()
                        
                        # Update cache
                        cls._cached_silk_data = silk_data
                        cls._cached_silk_data_timestamp = current_time
                        
                        # Also store in Redis for persistence across instances
                        cache.set('monitoring_silk_data', silk_data, cls.SILK_DATA_CACHE_INTERVAL * 2)
                    
                    await asyncio.sleep(cls.SILK_DATA_CACHE_INTERVAL)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"Error in Silk data refresh loop: {e}")
                    await asyncio.sleep(5)  # Wait longer on error
        except asyncio.CancelledError:
            print("Silk data refresh task cancelled")

    @classmethod
    @sync_to_async
    def get_silk_data_sync(cls):
        try:
            # Try to get from Redis first (in case this is a new instance)
            redis_data = cache.get('monitoring_silk_data')
            if redis_data and time.time() - cls._cached_silk_data_timestamp < cls.SILK_DATA_CACHE_INTERVAL:
                return redis_data
            
            recent_requests = SilkRequest.objects.order_by('-start_time')[:20]
            
            requests_data = []
            total_time = 0
            num_queries = 0
            
            # Calculate error rates by path
            error_paths = Counter()
            status_counts = Counter()
            all_paths = Counter()
            
            # Get the last 100 requests to calculate error rates
            for request in SilkRequest.objects.order_by('-start_time')[:100]:
                path_key = request.path.split('/')[1] or 'root'
                all_paths[path_key] += 1
                
                status_code = 0
                if hasattr(request, 'response') and request.response:
                    status_code = request.response.status_code
                    status_counts[status_code] += 1
                    
                    if status_code >= 400:
                        error_paths[path_key] += 1
            
            # Calculate error rates
            error_rates = {}
            for path, count in all_paths.items():
                error_rates[path] = round((error_paths[path] / count) * 100, 2) if count > 0 else 0
            
            # Get top 5 error paths
            top_error_paths = sorted(error_rates.items(), key=lambda x: x[1], reverse=True)[:5]
            
            # Status code distribution
            status_distribution = {
                "2xx": sum(count for status, count in status_counts.items() if 200 <= status < 300),
                "3xx": sum(count for status, count in status_counts.items() if 300 <= status < 400),
                "4xx": sum(count for status, count in status_counts.items() if 400 <= status < 500),
                "5xx": sum(count for status, count in status_counts.items() if 500 <= status < 600)
            }
            
            # Get slow SQL queries
            slow_queries = []
            query_types = Counter()
            with transaction.atomic():
                for query in SQLQuery.objects.order_by('-time_taken')[:10]:
                    query_text = query.query.strip().upper()
                    
                    # Determine query type
                    query_type = None
                    if query_text.startswith('SELECT'):
                        query_type = 'SELECT'
                    elif query_text.startswith('INSERT'):
                        query_type = 'INSERT'
                    elif query_text.startswith('UPDATE'):
                        query_type = 'UPDATE'
                    elif query_text.startswith('DELETE'):
                        query_type = 'DELETE'
                    else:
                        query_type = 'OTHER'
                    
                    query_types[query_type] += 1
                    
                    # Extract table names from query (simplified approach)
                    tables = []
                    try:
                        # Naive extraction of table names
                        if 'FROM' in query_text:
                            from_part = query_text.split('FROM')[1].split('WHERE')[0] if 'WHERE' in query_text else query_text.split('FROM')[1]
                            table_part = from_part.strip().split()[0].strip(',;')
                            tables.append(table_part)
                    except:
                        pass
                    
                    # Truncate long queries for display
                    display_query = query.query[:150] + "..." if len(query.query) > 150 else query.query
                    
                    slow_queries.append({
                        'query': display_query,
                        'time_taken': round(query.time_taken, 2),
                        'tables': tables,
                        'query_type': query_type
                    })
            
            # Process detailed request data
            for request in recent_requests:
                time_taken = request.time_taken or 0
                total_time += time_taken
                num_queries += request.num_sql_queries or 0
                
                status_code = None
                if hasattr(request, 'response') and request.response:
                    status_code = request.response.status_code
                
                # Calculate timing breakdown
                db_time = request.time_spent_on_sql_queries or 0
                view_time = time_taken - db_time
                
                # Add middleware and template rendering time if available
                middleware_time = getattr(request, 'time_spent_in_middleware', 0) or 0
                template_time = getattr(request, 'time_spent_in_templates', 0) or 0
                
                # Adjust view time (this is an approximation)
                view_time = max(0, view_time - middleware_time - template_time)
                
                requests_data.append({
                    'path': request.path,
                    'method': request.method,
                    'time_taken': round(time_taken, 2),
                    'num_queries': request.num_sql_queries or 0,
                    'time_db': round(db_time, 2),
                    'time_view': round(view_time, 2),
                    'time_middleware': round(middleware_time, 2),
                    'time_templates': round(template_time, 2),
                    'status_code': status_code or 0,
                    'encoded_url': request.encoded_url if hasattr(request, 'encoded_url') else request.path,
                    'timestamp': request.start_time.strftime('%H:%M:%S') if request.start_time else 'Unknown'
                })

            result = {
                'requests': requests_data,
                'avg_response_time': round(total_time / len(recent_requests), 2) if recent_requests else 0,
                'avg_queries_per_request': round(num_queries / len(recent_requests), 2) if recent_requests else 0,
                'error_analysis': {
                    'top_error_paths': top_error_paths,
                    'status_distribution': status_distribution
                },
                'sql_analysis': {
                    'slow_queries': slow_queries,
                    'query_types': dict(query_types)
                }
            }
            return result
        except Exception as e:
            print(f"Error getting Silk data: {e}")
            traceback.print_exc()
            return {
                'requests': [],
                'avg_response_time': 0,
                'avg_queries_per_request': 0,
                'error_analysis': {'top_error_paths': [], 'status_distribution': {}},
                'sql_analysis': {'slow_queries': [], 'query_types': {}}
            }

    @classmethod
    @sync_to_async
    def get_db_stats_sync(cls):
        try:
            # Try to get from Redis first (in case this is a new instance)
            redis_data = cache.get('monitoring_db_metrics')
            if redis_data and time.time() - cls._cached_db_metrics_timestamp < cls.DB_METRICS_CACHE_INTERVAL:
                return redis_data
                
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT count(*) 
                    FROM pg_stat_activity 
                    WHERE state = 'active'
                """)
                result = cursor.fetchone()
                active_connections = result[0] if result else 0

                cursor.execute("""
                    SELECT count(*) 
                    FROM pg_stat_activity 
                    WHERE state = 'active' 
                    AND now() - query_start > interval '1 second'
                """)
                result = cursor.fetchone()
                slow_queries = result[0] if result else 0

                cursor.execute("SHOW max_connections")
                result = cursor.fetchone()
                max_connections = result[0] if result else 0

                return {
                    'active_connections': active_connections,
                    'slow_queries': slow_queries,
                    'connection_pool': f"{active_connections}/{max_connections}",
                    'pool_usage_percent': round((active_connections / int(max_connections)) * 100, 2) if max_connections and int(max_connections) > 0 else 0
                }
        except Exception as e:
            print(f"Error getting DB stats: {e}")
            return {
                'active_connections': 0,
                'slow_queries': 0,
                'connection_pool': "0/0",
                'pool_usage_percent': 0
            }

    async def send_metrics(self):
        """Periodically send incremental metrics to client"""
        try:
            while self.is_connected:
                try:
                    # Get current metrics
                    current_metrics = await self.get_full_metrics()
                    
                    # Only calculate the diff for system metrics, which changes most frequently
                    # For other metrics, we'll check if they've changed at all
                    if self.last_sent_metrics:
                        # Create a data structure just for the incremental update
                        update = {
                            "timestamp": time.time(),
                            "metrics": {}
                        }
                        
                        # System metrics - always include as they change frequently
                        update["metrics"]["system"] = {
                            "cpu_usage": current_metrics["system"]["cpu_usage"],
                            "memory": {
                                "percent": current_metrics["system"]["memory"]["percent"],
                                "used": current_metrics["system"]["memory"]["used"],
                                "available": current_metrics["system"]["memory"]["available"],
                                "total": current_metrics["system"]["memory"]["total"]
                            },
                            "network": current_metrics["system"]["network"]
                        }
                        
                        # For database metrics, only include if changed
                        if current_metrics["database"] != self.last_sent_metrics["database"]:
                            update["metrics"]["database"] = current_metrics["database"]
                        
                        # For application metrics, only include if changed
                        if current_metrics["application"] != self.last_sent_metrics["application"]:
                            update["metrics"]["application"] = current_metrics["application"]
                        
                        # For profiling, check if avg_response_time changed
                        current_time = time.time()
                        if not hasattr(self, 'last_profiling_update') or current_time - self.last_profiling_update >= 15:
                            update["metrics"]["profiling"] = current_metrics["profiling"]
                            self.last_profiling_update = current_time
                        # If something important changed, also send it immediately
                        elif (current_metrics["profiling"]["avg_response_time"] != 
                            self.last_sent_metrics["profiling"]["avg_response_time"]):
                            update["metrics"]["profiling"] = current_metrics["profiling"]
                            self.last_profiling_update = current_time
                        
                        # Only send if we have connections and there's data to send
                        if self.is_connected and len(update["metrics"]) > 0:
                            await self.send(text_data=json.dumps({
                                "incrementalUpdate": True,
                                "data": update
                            }))
                    
                    # Update last sent metrics
                    self.last_sent_metrics = current_metrics
                    
                    await asyncio.sleep(self.SYSTEM_METRICS_INTERVAL)
                    
                except asyncio.CancelledError:
                    # Re-raise to handle task cancellation
                    raise
                except Exception as e:
                    print(f"Error in send_metrics loop: {e}")
                    if self.is_connected:
                        await asyncio.sleep(2)  # Wait before retrying
                    else:
                        break  # Exit the loop if disconnected
                    
        except asyncio.CancelledError:
            print("Metrics sending task was cancelled")
        except Exception as e:
            print(f"Unexpected error in send_metrics task: {e}")