# quickstart/sitemaps.py

from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from .models import ClassesMain


class StaticViewSitemap(Sitemap):
    """
    Sitemap for the main static pages of the site that are rendered by React.
    These URLs are important for navigation and should be crawled regularly.
    """

    priority = 0.8
    changefreq = "weekly"

    def items(self):
        return [
            "homepage",
            "explore",
            "business-welcome",
            "careers",
            "about-us",
            "giftcard",
            "terms-of-service",
            "privacy-policy",
        ]

    def location(self, item):
        return reverse(item)


class ClassSitemap(Sitemap):
    """
    Sitemap for all individual, publicly accessible class pages.
    This is the most important sitemap for discovering your core content.
    """

    changefreq = "weekly"
    priority = 0.9

    def items(self):
        return ClassesMain.objects.filter(
            status="active",
            businessId__isActive=True,
            businessId__verificationStatus="verified",
        ).order_by("-updatedAt")

    def lastmod(self, obj):
        return obj.updatedAt

    def location(self, obj):
        return f"/classes/{obj.slug}"
