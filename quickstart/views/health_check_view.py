# quickstart/views/health_check_view.py
from django.http import HttpResponse


def health_check(request):
    """
    A minimal, unauthenticated health check endpoint for the ALB.
    It returns a simple 200 OK response and does not perform
    any complex checks, including Host header validation if configured correctly.
    """
    return HttpResponse("OK", status=200)
