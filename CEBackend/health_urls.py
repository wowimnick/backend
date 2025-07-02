# CEBackend/health_urls.py
from django.urls import path
from quickstart.views.health_check_view import health_check

urlpatterns = [
    path("health-check/", health_check, name="health-check"),
]
