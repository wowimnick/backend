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
    changefreq = 'weekly'

    def items(self):
        # This list corresponds to the `name` of the URL patterns you will define in urls.py.
        # It's the cleanest way to manage static URLs.
        return [
            'homepage',
            'explore',
            'business-welcome',
            'careers',
            'about-us',
            'giftcard',
            'terms-of-service',
            'privacy-policy'
        ]

    def location(self, item):
        # The `reverse` function looks up the URL pattern by its name.
        return reverse(item)

class ClassSitemap(Sitemap):
    """
    Sitemap for all individual, publicly accessible class pages.
    This is the most important sitemap for discovering your core content.
    """
    changefreq = "weekly"
    priority = 0.9  # Individual class pages are very important.

    def items(self):
        # This query efficiently fetches all classes that should be public.
        return ClassesMain.objects.filter(
            status='active',
            businessId__isActive=True,
            businessId__verificationStatus='verified'
        ).order_by('-updatedAt') # Order by most recently updated

    def lastmod(self, obj):
        # Providing the last modification date is a strong signal to crawlers.
        return obj.updatedAt

    def location(self, obj):
        # This generates the correct frontend URL, e.g., /class/123/
        # It MUST match the route defined in your React App.jsx.
        return f'/class/{obj.classId}'

# If you add public business profile pages in the future, you can add a sitemap for them here.
# class BusinessSitemap(Sitemap):
#     ...