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
from silk.models import Request as SilkRequest
from django.db import connection

class MetricsConsumer(AsyncWebsocketConsumer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prev_network = None

    async def connect(self):
        """Handle initial connection"""
        print("Client attempting to connect")
        await self.accept()
        print("Client connected successfully")
        self.send_task = asyncio.create_task(self.send_metrics())

    async def disconnect(self, close_code):
        """Handle disconnection"""
        if hasattr(self, 'send_task'):
            self.send_task.cancel()
            try:
                await self.send_task
            except asyncio.CancelledError:
                pass

    async def receive(self, text_data):
        """Handle received messages"""
        pass

    @sync_to_async
    def get_silk_data_sync(self):
        try:
            recent_requests = SilkRequest.objects.order_by('-start_time')[:10]
            
            requests_data = []
            total_time = 0
            num_queries = 0
            
            for request in recent_requests:
                time_taken = request.time_taken or 0
                total_time += time_taken
                num_queries += request.num_sql_queries or 0
                
                # Get status code from response if available
                status_code = None
                if hasattr(request, 'response') and request.response:
                    status_code = request.response.status_code
                
                requests_data.append({
                    'path': request.path,
                    'method': request.method,
                    'time_taken': round(time_taken, 2),
                    'num_queries': request.num_sql_queries or 0,
                    'time_db': round(request.time_spent_on_sql_queries or 0, 2),
                    'status_code': status_code or 0
                })

            return {
                'requests': requests_data,
                'avg_response_time': round(total_time / len(recent_requests), 2) if recent_requests else 0,
                'avg_queries_per_request': round(num_queries / len(recent_requests), 2) if recent_requests else 0
            }
        except Exception as e:
            print(f"Error getting Silk data: {e}")
            return {'requests': [], 'avg_response_time': 0, 'avg_queries_per_request': 0}
    
    @sync_to_async
    def get_db_stats_sync(self):
        """Get database statistics"""
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT count(*) 
                    FROM pg_stat_activity 
                    WHERE state = 'active'
                """)
                active_connections = cursor.fetchone()[0]

                cursor.execute("""
                    SELECT count(*) 
                    FROM pg_stat_activity 
                    WHERE state = 'active' 
                    AND now() - query_start > interval '1 second'
                """)
                slow_queries = cursor.fetchone()[0]

                cursor.execute("SHOW max_connections")
                max_connections = cursor.fetchone()[0]

                return {
                    'active_connections': active_connections,
                    'slow_queries': slow_queries,
                    'connection_pool': f"{active_connections}/{max_connections}",
                    'pool_usage_percent': round((active_connections / int(max_connections)) * 100, 2)
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
        """Periodically send metrics"""
        while True:
            try:
                # Get current system metrics
                cpu_times = psutil.cpu_times_percent()
                disk = psutil.disk_usage('/')
                network = psutil.net_io_counters()
                current_time = time.time()
                
                # Calculate network rates
                if self.prev_network is None:
                    # First reading, set initial values and send 0 for rates
                    network_rates = {
                        'bytes_sent': 0,
                        'bytes_recv': 0,
                        'packets_sent': network.packets_sent,
                        'packets_recv': network.packets_recv
                    }
                else:
                    time_diff = current_time - self.prev_network['timestamp']
                    network_rates = {
                        'bytes_sent': round((network.bytes_sent - self.prev_network['bytes_sent']) / time_diff),
                        'bytes_recv': round((network.bytes_recv - self.prev_network['bytes_recv']) / time_diff),
                        'packets_sent': network.packets_sent,
                        'packets_recv': network.packets_recv
                    }

                # Store current values for next calculation
                self.prev_network = {
                    'bytes_sent': network.bytes_sent,
                    'bytes_recv': network.bytes_recv,
                    'timestamp': current_time
                }
                
                metrics = {
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
                    "database": await self.get_db_stats_sync(),
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
                    "profiling": await self.get_silk_data_sync()
                }
                
                await self.send(text_data=json.dumps(metrics))
                await asyncio.sleep(1)  # Update every second
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in send_metrics: {e}")
                await asyncio.sleep(1)