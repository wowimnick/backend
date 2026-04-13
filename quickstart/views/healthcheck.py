import os
import logging
from django.http import HttpResponse, JsonResponse
from django.db import connection
from django.core.cache import cache
from django.conf import settings
import time

logger = logging.getLogger(__name__)


def health_check(request):
    """
    Enhanced health check that verifies critical services are working
    """
    try:
        # Basic response for ELB
        if request.method == "GET":
            # Quick check - just return OK for ELB
            return HttpResponse("OK", status=200)

        # Detailed health check for monitoring (optional)
        health_status = {"status": "healthy", "timestamp": time.time(), "checks": {}}

        # Database connectivity check
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            health_status["checks"]["database"] = "healthy"
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            health_status["checks"]["database"] = "unhealthy"
            health_status["status"] = "unhealthy"

        # Cache connectivity check
        try:
            cache.set("health_check", "test", 10)
            cache.get("health_check")
            health_status["checks"]["cache"] = "healthy"
        except Exception as e:
            logger.error(f"Cache health check failed: {e}")
            health_status["checks"]["cache"] = "unhealthy"
            health_status["status"] = "unhealthy"

        status_code = 200 if health_status["status"] == "healthy" else 503
        return JsonResponse(health_status, status=status_code)

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return HttpResponse("Service Unavailable", status=503)


def robots_txt_view(request):
    # Check the environment variable you set in your deployment script
    is_staging = os.environ.get("DJANGO_ENV") == "staging"

    if is_staging:
        lines = ["User-agent: *", "Disallow: /"]
    else:
        # For production, we allow everything and advertise the sitemap.
        lines = [
            "# Classeasily Robots Rules",
            "#   /$$$$$$  /$$                                                           /$$ /$$          ",
            "#  /$$__  $$| $$                                                           |__/| $$          ",
            r"# | $$  \__/| $$  /$$$$$$   /$$$$$$$ /$$$$$$$  /$$$$$$   /$$$$$$   /$$$$$$$ /$$| $$ /$$   /$$",
            "# | $$      | $$ |____  $$ /$$_____//$$_____/ /$$__  $$ |____  $$ /$$_____/| $$| $$| $$  | $$",
            "# | $$      | $$  /$$$$$$$|  $$$$$$|  $$$$$$ | $$$$$$$$  /$$$$$$$|  $$$$$$ | $$| $$| $$  | $$",
            r"# | $$    $$| $$ /$$__  $$ \____  $$\____  $$| $$_____/ /$$__  $$ \____  $$| $$| $$| $$  | $$",
            "# |  $$$$$$/| $$|  $$$$$$$ /$$$$$$$//$$$$$$$/|  $$$$$$$|  $$$$$$$ /$$$$$$$/| $$| $$|  $$$$$$$",
            r"#  \______/ |__/ \_______/|_______/|_______/  \_______/ \_______/|_______/ |__/|__/ \____  $$",
            "#                                                                                   /$$  | $$",
            "#                                                                                  |  $$$$$$/",
            r"#                                                                                   \______/ ",
            "#",
            "#",
            "User-agent: *",
            "Allow: /",
            "",
            "Disallow: /admin/",
            "Disallow: /api/",
            "Disallow: /login",
            "Disallow: /auth/",
            "Disallow: /verify-email/",
            "Disallow: /reset-password/",
            "Disallow: /account/settings",
            "Disallow: /my-tickets/",
            "Disallow: /my-classes/",
            "Disallow: /my-messages/",
            "Disallow: /guest-inbox/",
            "Disallow: /booking/status/",
            "Disallow: /stripe-connect/",
            "",
            "# Allow business root and business profile pages",
            "Allow: /business$",
            "Allow: /business/$",
            "Allow: /business/*/$",
            "# Disallow all other business paths",
            "Disallow: /business/*/",
            "",
            "# AI / research crawlers (public marketing content only)",
            "User-agent: GPTBot",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /api/",
            "Disallow: /my-",
            "Disallow: /guest-inbox/",
            "Disallow: /booking/",
            "Disallow: /stripe-connect/",
            "",
            "User-agent: ChatGPT-User",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /api/",
            "",
            "User-agent: ClaudeBot",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /api/",
            "",
            "User-agent: Google-Extended",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /api/",
            "",
            "# Machine-readable site summary for AI assistants",
            "Sitemap: https://classeasily.com/sitemap.xml",
        ]

    return HttpResponse("\n".join(lines), content_type="text/plain")
