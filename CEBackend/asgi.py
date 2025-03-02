# CEBackend/asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.security.websocket import AllowedHostsOriginValidator

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'CEBackend.settings')
django_asgi_app = get_asgi_application()

# Import these AFTER django setup
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.urls import path
from quickstart.monitoring.consumers import MetricsConsumer

websocket_urlpatterns = [
    path('ws/system_metrics/', MetricsConsumer.as_asgi()),
]

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns)
        )
    ),
})