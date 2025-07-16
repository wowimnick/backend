from django.http import HttpResponse


def health_check(request):
    """A simple view for the Load Balancer health check."""
    return HttpResponse("OK")


def robots_txt_view(request):
    # The content of your robots.txt file.
    # It's better to manage this here in code than as a loose file.
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
        # IMPORTANT: Make sure this points to your final PRODUCTION domain
        "Sitemap: https://www.classeasily.com/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")
