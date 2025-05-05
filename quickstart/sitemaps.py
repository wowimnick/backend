# quickstart/sitemaps.py
from django.contrib.sitemaps import Sitemap
from django.urls import reverse # Keep reverse if you have *any* named Django URLs for static pages
from django.utils import timezone
from .models import ClassesMain, BusinessInfo

# --- Static Frontend Routes Sitemap ---
class StaticViewSitemap(Sitemap):
    """Sitemap for main static/navigational frontend routes."""
    priority = 0.8 # Set a reasonably high priority for main navigation pages
    changefreq = 'weekly' # Assume static pages don't change extremely often

    def items(self):
        # List the absolute paths of your main frontend routes from App.jsx
        return [
            '/',                 # Homepage
            '/explore',          # Explore Page
            '/business',         # Business Welcome Page
            '/careers',          # Careers Landing Page
            '/careers/positions',# Career Positions Page
            '/about-us',         # About Us Page
            # Add other static pages you want indexed:
            # '/help',
            # '/gift-cards',
            # '/terms-of-service',
            # '/privacy-policy',
        ]

    def location(self, item_path):
        # For these static frontend paths, the location is the path itself
        return item_path

    # Optional: Define lastmod if you want to signal updates
    # def lastmod(self, item_path):
    #     # You could return a fixed date or fetch update times from a CMS/DB
    #     # For simple static pages, omitting lastmod is often fine.
    #     return timezone.now().date()

# --- Class Pages Sitemap (Dynamically Generated) ---
class ClassSitemap(Sitemap):
    """Sitemap for individual, active Class pages."""
    changefreq = "weekly" # How often class details might change
    priority = 0.7 # Slightly lower than main pages, but higher than default

    def items(self):
        # Return a queryset of *publicly accessible* class objects
        return ClassesMain.objects.filter(
            status='active',
            businessId__isActive=True,
            businessId__verificationStatus='verified'
        ).order_by('-updatedAt')

    def lastmod(self, obj):
        # Use the 'updatedAt' timestamp from the class model
        return obj.updatedAt

    def location(self, obj):
        # Construct the frontend URL path for a class
        # Matches the '/class/:id' route in App.jsx
        return f'/class/{obj.classId}'

# --- Optional: Business Profile Sitemap ---
# Uncomment and implement if you create public-facing pages for businesses
# class BusinessProfileSitemap(Sitemap):
#     changefreq = "monthly"
#     priority = 0.6
#
#     def items(self):
#         return BusinessInfo.objects.filter(isActive=True, verificationStatus='verified')
#
#     def lastmod(self, obj):
#         return obj.updatedAt
#
#     def location(self, obj):
#         # IMPORTANT: Define the frontend route for business profiles
#         # Example: return f'/business-profile/{obj.businessId}'
#         pass # Replace pass with your actual URL logic

# --- You generally DO NOT want to include these in the sitemap ---
# Routes requiring login or specific permissions (like dashboards, settings, my-tickets)
# Authentication process routes (/verify-email, /reset-password)
# Catch-all route (*)
# API endpoints