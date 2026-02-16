# CEBackend/asgi.py
import os
from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "CEBackend.settings")

_django_app = get_asgi_application()


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
    await _django_app(scope, receive, send)