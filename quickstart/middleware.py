import os
from django.conf import settings
from django.http import HttpResponse
import logging
from django.utils.functional import SimpleLazyObject
from quickstart.models import BusinessInfo

logger = logging.getLogger(__name__)


def get_business(request):
    if hasattr(request, "_cached_business"):
        return request._cached_business

    business_key = request.headers.get("X-Business-ID")
    if not business_key:
        request._cached_business = None
        return None

    try:
        business = BusinessInfo.objects.get(widget_api_key=business_key)
        request._cached_business = business
        return business
    except (BusinessInfo.DoesNotExist, ValueError):
        request._cached_business = None
        return None


class DynamicCorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Attach the lazy business object to every request for potential use.
        request.business_context = SimpleLazyObject(lambda: get_business(request))

        # --- STAGE 1: Handle the Preflight (OPTIONS) Request ---
        # This block runs *before* the actual view is processed.
        if request.method == "OPTIONS" and request.path.startswith("/api/widget/v1/"):
            origin = request.headers.get("Origin")
            if origin:
                # For preflight, we create a blank response and add headers to it.
                response = HttpResponse(status=200)

                # We "provisionally" allow the origin. The real check happens on the actual request.
                response["Access-Control-Allow-Origin"] = origin

                # We explicitly state which headers and methods are allowed.
                response["Access-Control-Allow-Headers"] = "X-Business-ID, Content-Type"
                response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

                logger.debug(f"Handled preflight request from origin: {origin}")
                return response

        # --- STAGE 2: Handle the Actual (GET, POST) Request ---
        # For non-preflight requests, we process the view first to get the response.
        response = self.get_response(request)

        # Now, we add the CORS header to the *actual* response, but only if it's a valid widget request.
        if request.path.startswith("/api/widget/v1/"):
            origin = request.headers.get("Origin")
            if origin:
                # Allow localhost / 127.0.0.1 (any port) for development and testing.
                _host = (
                    origin.replace("https://", "")
                    .replace("http://", "")
                    .split("/")[0]
                    .split(":")[0]
                )
                if _host in ("localhost", "127.0.0.1"):
                    response["Access-Control-Allow-Origin"] = origin
                    logger.debug(f"Added CORS header for dev origin: {origin}")
                else:
                    business = request.business_context
                    if business and business.allowed_widget_origins:
                        origin_domain = (
                            origin.replace("https://", "")
                            .replace("http://", "")
                            .split("/")[0]
                        )
                        normalized_allowed_origins = [
                            o.replace("https://", "").replace("http://", "").rstrip("/")
                            for o in business.allowed_widget_origins
                        ]
                        if origin_domain in normalized_allowed_origins:
                            response["Access-Control-Allow-Origin"] = origin
                            logger.debug(
                                f"Added CORS header for valid origin: {origin} for business {business.businessId}"
                            )
                        else:
                            logger.warning(
                                f"CORS REJECTED: Origin '{origin_domain}' not in allowed list for business {business.businessId}"
                            )

        return response


class JWTCookieMiddleware:
    """
    Extracts JWT token from httpOnly cookie and adds it to the Authorization header.
    This allows DRF's JWT authentication to work with httpOnly cookies.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Try to get JWT from cookie
        access_token = request.COOKIES.get(settings.SIMPLE_JWT["AUTH_COOKIE"])

        # If token exists in cookie, add it to the Authorization header
        if access_token:
            request.META["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"

        response = self.get_response(request)
        return response


# Normalized health-check paths (with or without trailing slash).
_HEALTH_CHECK_PATHS = frozenset({"/health-check", "/api/health-check", "/"})


class HealthCheckMiddleware:
    """
    Returns 200 for health-check requests before any other middleware or views run.
    Matches with or without trailing slash so ALB never gets 302/404.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = (request.path or "/").rstrip("/") or "/"
        if path in _HEALTH_CHECK_PATHS:
            try:
                response = HttpResponse("OK", status=200)
                response["X-Health-Check"] = "OK"
                response["X-Container-ID"] = request.META.get("HOSTNAME", "unknown")
                return response
            except Exception:
                return HttpResponse("OK", status=200)

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


class DisableCSRFForJWTMiddleware:
    """
    Disable CSRF checks for JWT-authenticated requests.
    Since JWT tokens are in httpOnly cookies and validated on every request,
    CSRF protection is redundant and causes issues.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip CSRF for all API endpoints using JWT
        if request.path.startswith("/api/"):
            setattr(request, "_dont_enforce_csrf_checks", True)

        response = self.get_response(request)
        return response
