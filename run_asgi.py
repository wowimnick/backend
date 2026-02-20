#!/usr/bin/env python
"""
Run the backend with ASGI (uvicorn) so WebSockets work.
Use this instead of `manage.py runserver` when you need:
  - /api/ws/notifications/
  - /api/ws/conversations/<id>/

Usage (from backend repo root):
  python run_asgi.py
  # or: python run_asgi.py --port 8001
"""
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "CEBackend.settings")
    port = "8000"
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv):
            port = sys.argv[i + 1]
    import uvicorn
    uvicorn.run(
        "CEBackend.asgi:application",
        host="0.0.0.0",
        port=int(port),
        lifespan="off",
    )
