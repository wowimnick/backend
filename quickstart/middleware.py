import os
from django.conf import settings
from django.db import models
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
import logging
from django.core.cache import cache
from django.utils.functional import SimpleLazyObject
from quickstart.models import BusinessInfo, BannedIP
from quickstart.utils.request_utils import get_client_ip

logger = logging.getLogger(__name__)

# Reserved widget key: when X-Business-ID is this, no DB lookup; views return mock data and checkout is disabled.
WIDGET_DEMO_KEY = "demo"


class DemoBusinessContext:
    """Sentinel for demo mode. No real business; config/classes/availability are mock; booking disabled."""
    is_demo = True


def get_business(request):
    if hasattr(request, "_cached_business"):
        return request._cached_business

    raw_key = request.headers.get("X-Business-ID")
    if not raw_key:
        request._cached_business = None
        return None

    # Use only the key part; ignore any query string (e.g. "demo?ce_plan=uuid")
    business_key = raw_key.split("?")[0].strip()

    if business_key == WIDGET_DEMO_KEY:
        request._cached_business = DemoBusinessContext()
        return request._cached_business

    try:
        business = BusinessInfo.objects.get(widget_api_key=business_key)
        request._cached_business = business
        return business
    except (BusinessInfo.DoesNotExist, ValueError):
        request._cached_business = None
        return None


_WIDGET_PAYMENT_PATHS = (
    "/api/payments/update-payment-intent/",
    "/api/payments/cancel-payment-intent/",
    "/payments/update-payment-intent/",
    "/payments/cancel-payment-intent/",
)


def _is_widget_path(path):
    # Widget API routes + shared payment endpoints the widget calls cross-origin
    if path.startswith("/api/widget/v1/") or path.startswith("/widget/v1/"):
        return True
    return path in _WIDGET_PAYMENT_PATHS


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


# Website builders that host embed content on a different origin than the customer's site URL.
# When a business has Allowed Domains set (e.g. their Wix/Squarespace site), we allow requests from these
# embed host suffixes so the widget works when embedded. Each suffix is the trailing part of the host
# (e.g. ".filesusr.com" matches "6c3e658f-7198-48d1-9d02-32dcf9b6a3d7.filesusr.com").
# Use lowercase, include leading dot so we match subdomains only (e.g. ".carrd.co" not "carrd.co").
WIDGET_EMBED_HOST_SUFFIXES = (
    ".filesusr.com",        # Wix: embeds run from *.filesusr.com, not wixsite.com
    ".webflow.io",          # Webflow: published sites
    ".canvas.webflow.com",  # Webflow: preview/editor
    ".squarespace.com",     # Squarespace: code blocks / embed run on site origin
    ".mystrikingly.com",    # Strikingly: embed content (e.g. embed.mystrikingly.com)
    ".weebly.com",          # Weebly
    ".square.site",         # Square Online (Weebly successor)
    ".carrd.co",            # Carrd
    ".sites.google.com",    # Google Sites
)


def _is_known_embed_host(origin):
    """
    True if origin is a known website-builder embed host (e.g. Wix filesusr.com, Webflow, Squarespace).
    These hosts serve embedded widget content from their domain instead of the customer's site URL.
    """
    if not origin:
        return False
    domain = _normalize_origin_domain(origin)
    if not domain:
        return False
    domain_lower = domain.lower()
    return any(domain_lower.endswith(suffix) for suffix in WIDGET_EMBED_HOST_SUFFIXES)


def _origin_allowed_for_business(origin, business):
    """
    Return True if this Origin is allowed for this business.
    - localhost/127.0.0.1: allowed (dev).
    - classeasily.com or *.classeasily.com: allowed (our app/widget-demo).
    - Demo key: only localhost and classeasily domains (no customer domains).
    - Known embed hosts (Wix, Webflow, Squarespace, etc.): allowed if business has any Allowed Domains.
    - Any other domain: allowed only if listed in this business's Allowed Domains (allowed_widget_origins).
    """
    if not origin:
        return False
    origin_domain = _normalize_origin_domain(origin)
    if origin_domain in ("localhost", "127.0.0.1"):
        return True
    if _is_classeasily_domain(origin):
        return True
    if getattr(business, "is_demo", False):
        return False  # demo key only allowed from our app / localhost
    if not business or not business.allowed_widget_origins:
        return False
    # Website builders often serve embeds from their CDN (e.g. Wix filesusr.com); allow when business
    # has configured allowed domains (e.g. their Wix/Squarespace site URL).
    if _is_known_embed_host(origin):
        return True
    normalized_allowed = [
        o.replace("https://", "").replace("http://", "").rstrip("/").split(":")[0].lower()
        for o in business.allowed_widget_origins
    ]
    return (origin_domain or "").lower() in normalized_allowed


BANNED_IP_CACHE_KEY = "banned_ip_addresses"
BANNED_IP_CACHE_TTL = 60


def _get_active_banned_ips():
    cached = cache.get(BANNED_IP_CACHE_KEY)
    if cached is not None:
        return cached

    now = timezone.now()
    ips = set(
        BannedIP.objects.filter(is_active=True)
        .filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))
        .values_list("ip_address", flat=True)
    )
    cache.set(BANNED_IP_CACHE_KEY, ips, BANNED_IP_CACHE_TTL)
    return ips


def invalidate_banned_ip_cache():
    cache.delete(BANNED_IP_CACHE_KEY)


class BannedIPMiddleware:
    """Return 403 for banned client IPs on /api/ routes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ""
        if path.startswith("/api/"):
            client_ip = get_client_ip(request)
            if client_ip and client_ip in _get_active_banned_ips():
                return JsonResponse({"detail": "Access denied."}, status=403)
        return self.get_response(request)


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
            # Origin can be in request.headers (Django 4+) or request.META (all versions / some proxies)
            origin = request.headers.get("Origin") if hasattr(request, "headers") else None
            if not origin:
                origin = request.META.get("HTTP_ORIGIN")
            if origin:
                response["Access-Control-Allow-Origin"] = origin
                logger.debug("Preflight: ACAO set for origin %s", _normalize_origin_domain(origin))
            else:
                # Preflight without Origin is unusual; mirror * so browser gets some ACAO (actual GET/POST still validated below)
                response["Access-Control-Allow-Origin"] = "*"
                logger.warning("Preflight: no Origin header for path %s, using *", request.path)
            response["Vary"] = "Origin"
            return response

        # --- STAGE 2: Actual (GET, POST) — reject invalid origins before running the view ---
        # This prevents widget data from being returned to disallowed origins at all.
        if request.method in ("GET", "POST") and _is_widget_path(request.path):
            origin = request.headers.get("Origin") if hasattr(request, "headers") else None
            if not origin:
                origin = request.META.get("HTTP_ORIGIN")
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
            origin = request.headers.get("Origin") if hasattr(request, "headers") else None
            if not origin:
                origin = request.META.get("HTTP_ORIGIN")
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
