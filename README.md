# backend

## Running locally

- **HTTP only (no WebSockets):**  
  `python manage.py runserver`

- **With WebSockets (notifications, chat):**  
  `python run_asgi.py`  
  or: `uvicorn CEBackend.asgi:application --host 0.0.0.0 --port 8000 --lifespan off`

Use the ASGI server when the frontend connects to `/api/ws/notifications/` or conversation WebSockets; otherwise you’ll see 404 for those paths.

