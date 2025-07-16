from django.http import HttpResponse
from django.conf import settings


def health_check(request):
    """A simple view for the Load Balancer health check."""
    return HttpResponse("OK")


def robots_txt_view(request):
    # Check the environment variable you set in your deployment script
    is_staging = settings.DJANGO_ENV == "staging"

    if is_staging:
        lines = ["User-agent: *", "Disallow: /"]
    else:
        # For production, we allow everything and advertise the sitemap.
        lines = [
            "# Classeasily Robots Rules",
            "#   /$$$$$$  /$$                                                            /$$ /$$          ",
            "#  /$$__  $$| $$                                                           |__/| $$          ",
            "# | $$  \__/| $$  /$$$$$$   /$$$$$$$ /$$$$$$$  /$$$$$$   /$$$$$$   /$$$$$$$ /$$| $$ /$$   /$$",
            "# | $$      | $$ |____  $$ /$$_____//$$_____/ /$$__  $$ |____  $$ /$$_____/| $$| $$| $$  | $$",
            "# | $$      | $$  /$$$$$$$|  $$$$$$|  $$$$$$ | $$$$$$$$  /$$$$$$$|  $$$$$$ | $$| $$| $$  | $$",
            "# | $$    $$| $$ /$$__  $$ \____  $$\____  $$| $$_____/ /$$__  $$ \____  $$| $$| $$| $$  | $$",
            "# |  $$$$$$/| $$|  $$$$$$$ /$$$$$$$//$$$$$$$/|  $$$$$$$|  $$$$$$$ /$$$$$$$/| $$| $$|  $$$$$$$",
            "#  \______/ |__/ \_______/|_______/|_______/  \_______/ \_______/|_______/ |__/|__/ \____  $$",
            "#                                                                                   /$$  | $$",
            "#                                                                                  |  $$$$$$/",
            "#                                                                                   \______/ ",
            "#",
            "#",
            "User-agent: *",
            "Allow: /",
            "",
            "Disallow: /admin/",
            "Disallow: /business/",
            "Disallow: /api/",
            "Disallow: /login",
            "Disallow: /auth/",
            "Disallow: /verify-email/",
            "Disallow: /reset-password/",
            "Disallow: /account/settings",
            "Disallow: /my-tickets/",
            "",
            "Sitemap: https://www.classeasily.com/sitemap.xml",
        ]

    return HttpResponse("\n".join(lines), content_type="text/plain")
