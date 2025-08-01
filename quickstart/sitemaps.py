# quickstart/sitemaps.py

from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from django.utils.text import slugify
from .models import ClassesMain, ClassCategory, ClassSubcategory, BusinessInfo


class StaticViewSitemap(Sitemap):
    """
    Sitemap for the main static pages of the site.
    The generic '/explore' is included here as a fallback entry point.
    """

    priority = 0.6
    changefreq = "monthly"

    def items(self):
        # Removed 'explore' as it's now handled by the dynamic sitemap.
        return [
            "homepage",
            "business-welcome",
            "careers",
            "about-us",
            "terms-of-service",
            "privacy-policy",
        ]

    def location(self, item):
        # These names must match the `name=` argument in your urls.py
        # Assuming you have named your URL patterns accordingly.
        # If not, you'd return the static path like '/about-us'
        try:
            return reverse(item)
        except:
            # Fallback for simple paths if reverse lookup fails
            return f'/{item.replace("-welcome", "")}'


class ClassSitemap(Sitemap):
    """
    Sitemap for all individual, publicly accessible class pages.
    This remains the most important sitemap for your core content.
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
        # This correctly points to your individual class pages.
        return f"/classes/{obj.slug}"


class ExplorePagesSitemap(Sitemap):
    """
    Dynamically generates sitemap entries for all key "explore" landing pages.
    This helps Google discover your main location and category-based pages.
    """
    changefreq = "daily"
    priority = 0.8

    def items(self):
        urls = {"/explore"}  # Use a set to avoid duplicates

        # Get all active categories and subcategories
        categories = ClassCategory.objects.prefetch_related('subcategories').all()
        
        # Get all unique, active city/province locations
        # We only want locations that actually have active classes
        active_business_locations = BusinessInfo.objects.filter(
            isActive=True, verificationStatus='verified'
        ).values_list('businessState', 'businessCity').distinct()

        locations = []
        for state, city in active_business_locations:
            if state and city:
                locations.append({
                    'province_slug': slugify(state),
                    'city_slug': slugify(city)
                })

        # --- Generate all URL combinations ---

        # 1. Category and Subcategory URLs (e.g., /explore/category/arts, /explore/category/arts/pottery)
        for cat in categories:
            urls.add(f"/explore/category/{cat.key}")
            for subcat in cat.subcategories.all():
                urls.add(f"/explore/category/{cat.key}/{subcat.key}")

        # 2. Location-based URLs
        for loc in locations:
            # e.g., /explore/ontario/toronto
            base_loc_path = f"/explore/{loc['province_slug']}/{loc['city_slug']}"
            urls.add(base_loc_path)

            # 3. Combined Location + Category URLs
            for cat in categories:
                # e.g., /explore/ontario/toronto/arts
                urls.add(f"{base_loc_path}/{cat.key}")
                for subcat in cat.subcategories.all():
                    # e.g., /explore/ontario/toronto/arts/pottery
                    urls.add(f"{base_loc_path}/{cat.key}/{subcat.key}")
        
        return list(urls)

    def location(self, item):
        # The 'item' is already the full URL path we generated
        return item