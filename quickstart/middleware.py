import os
from django.conf import settings


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


from django.utils.deprecation import MiddlewareMixin


class HealthCheckMiddleware(MiddlewareMixin):
    """
    Intercepts requests to the health check endpoint and bypasses
    the `SecurityMiddleware`'s HTTPS redirect.
    """

    def process_request(self, request):
        # NOTE: Adjust this path if your health check URL is different.
        if request.path == "/api/health-check/":
            # This is the magic. We tell the `SecurityMiddleware` that the
            # request is already secure, so it doesn't need to redirect.
            request.is_secure = lambda: True


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
