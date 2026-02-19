# CEBackend/asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "CEBackend.settings")

_django_app = get_asgi_application()

# Import routing after Django setup
from quickstart.routing import websocket_urlpatterns

_asgi_app = ProtocolTypeRouter(
    {
        "http": _django_app,
        "websocket": AllowedHostsOriginValidator(
            URLRouter(websocket_urlpatterns)
        ),
    }
)


async def application(scope, receive, send):
    """
    ASGI app that handles lifespan scope so uvicorn (and other ASGI servers)
    that send lifespan events do not trigger:
    ValueError: Django can only handle ASGI/HTTP connections, not lifespan.
    """
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return
    await _asgi_app(scope, receive, send)