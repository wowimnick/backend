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


def _is_widget_path(path):
    return path.startswith("/api/widget/v1/")


def _normalize_origin_domain(origin):
    """Extract host from Origin header (scheme + host, no path, no port for comparison)."""
    if not origin:
        return None
    return (
        origin.replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
        .split(":")[0]
    )


def _is_classeasily_domain(origin):
    """
    True only for classeasily.com or *.classeasily.com (e.g. staging.classeasily.com, www.classeasily.com).
    Used so our app and widget-demo work without being in a business's Allowed Domains.
    """
    if not origin:
        return False
    domain = _normalize_origin_domain(origin)
    if not domain:
        return False
    domain = domain.lower()
    return domain == "classeasily.com" or domain.endswith(".classeasily.com")


def _origin_allowed_for_business(origin, business):
    """
    Return True if this Origin is allowed for this business.
    - localhost/127.0.0.1: allowed (dev).
    - classeasily.com or *.classeasily.com: allowed (our app/widget-demo).
    - Any other domain: allowed only if listed in this business's Allowed Domains (allowed_widget_origins).
    So testsite.com works only for the business that has testsite.com in their box; other businesses get 403.
    """
    if not origin:
        return False
    origin_domain = _normalize_origin_domain(origin)
    if origin_domain in ("localhost", "127.0.0.1"):
        return True
    if _is_classeasily_domain(origin):
        return True
    if not business or not business.allowed_widget_origins:
        return False
    normalized_allowed = [
        o.replace("https://", "").replace("http://", "").rstrip("/").split(":")[0].lower()
        for o in business.allowed_widget_origins
    ]
    return (origin_domain or "").lower() in normalized_allowed


class DynamicCorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Attach the lazy business object to every request for potential use.
        request.business_context = SimpleLazyObject(lambda: get_business(request))

        # --- STAGE 1: Handle the Preflight (OPTIONS) Request ---
        # Browser does not send X-Business-ID on preflight, so we cannot look up business.
        # Allow preflight for any origin so that customer domains in Allowed Domains can send the actual request.
        # GET/POST still enforce: only classeasily.com/*.classeasily.com or origin in this business's Allowed Domains.
        if request.method == "OPTIONS" and _is_widget_path(request.path):
            response = HttpResponse(status=200)
            response["Access-Control-Allow-Headers"] = "X-Business-ID, Content-Type"
            response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            origin = request.headers.get("Origin")
            if origin:
                response["Access-Control-Allow-Origin"] = origin
                logger.debug("Preflight: ACAO set for origin %s", _normalize_origin_domain(origin))
            return response

        # --- STAGE 2: Actual (GET, POST) — reject invalid origins before running the view ---
        # This prevents widget data from being returned to disallowed origins at all.
        if request.method in ("GET", "POST") and _is_widget_path(request.path):
            origin = request.headers.get("Origin")
            if origin:
                business = request.business_context
                if not _origin_allowed_for_business(origin, business):
                    logger.warning(
                        "CORS REJECTED: Origin '%s' not allowed for business %s",
                        _normalize_origin_domain(origin),
                        getattr(business, "businessId", None),
                    )
                    return HttpResponse(
                        '{"detail":"Origin not allowed."}',
                        status=403,
                        content_type="application/json",
                    )

        response = self.get_response(request)

        # Add CORS header to the response only for allowed origins (GET/POST already validated above).
        if _is_widget_path(request.path):
            origin = request.headers.get("Origin")
            if origin:
                business = request.business_context
                if _origin_allowed_for_business(origin, business):
                    response["Access-Control-Allow-Origin"] = origin
                    logger.debug(
                        "Added CORS header for valid origin: %s",
                        origin,
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
