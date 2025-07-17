import os
from django.conf import settings
from django.http import HttpResponse


class JWTCookieMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Try to get JWT from cookie first
        access_token = request.COOKIES.get(settings.SIMPLE_JWT["AUTH_COOKIE"])

        # If token exists in cookie, add it to the Authorization header
        if access_token:
            request.META["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"

            # For non-GET, non-HEAD requests, ensure CSRF token is validated
            if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
                # Django's CSRF middleware will handle the validation
                # Just ensure we're not bypassing it
                pass

        response = self.get_response(request)
        return response


class HealthCheckMiddleware:
    """
    This middleware is designed to intercept health check requests from the
    AWS ELB at the earliest possible moment and return a successful response.
    It completely bypasses all other Django middleware (including the
    one that checks ALLOWED_HOSTS), ensuring that health checks are fast,
    reliable, and immune to Host header issues.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # The health check path configured in your ELB Target Group.
        # Change this if you change it in the Target Group.
        if request.path == "/api/health-check/":
            return HttpResponse("OK")
        if request.path == "/health-check/":
            return HttpResponse("OK")

        # If it's not a health check, let Django process the request normally.
        return self.get_response(request)


class SeoStagingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.is_staging = os.environ.get("DJANGO_ENV") == "staging"

    def __call__(self, request):
        response = self.get_response(request)

        # The rest of the logic is the same and is correct.
        if self.is_staging:
            response["X-Robots-Tag"] = "noindex, nofollow"

        return response
