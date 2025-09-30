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
        # Add the main /business landing page here
        return [
            "homepage",
            "business-welcome",  # This corresponds to the /business page
            "careers",
            "about-us",
            "terms-of-service",
            "privacy-policy",
        ]

    def location(self, item):
        try:
            return reverse(item)
        except:
            return f'/{item.replace("-welcome", "")}'


class ClassSitemap(Sitemap):
    """
    Sitemap for all individual, publicly accessible class pages.
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


# --- NEW: Sitemap for Business Pages ---
class BusinessSitemap(Sitemap):
    """
    Sitemap for all individual, publicly accessible business pages.
    """

    changefreq = "weekly"
    priority = 0.8

    def items(self):
        # Return a queryset of all businesses that should be indexed
        return BusinessInfo.objects.filter(
            isActive=True,
            verificationStatus="verified",
        ).order_by("-updatedAt")

    def lastmod(self, obj):
        # Use the updatedAt field for the last modification date
        return obj.updatedAt

    def location(self, obj):
        # Construct the URL using the business's slug
        return f"/business/{obj.slug}"


class ExplorePagesSitemap(Sitemap):
    """
    Dynamically generates sitemap entries for all key "explore" landing pages.
    """

    changefreq = "daily"
    priority = 0.8

    def items(self):
        urls = {"/explore"}  # Use a set to avoid duplicates

        categories = ClassCategory.objects.prefetch_related("subcategories").all()

        active_business_locations = (
            BusinessInfo.objects.filter(isActive=True, verificationStatus="verified")
            .values_list("businessState", "businessCity")
            .distinct()
        )

        locations = []
        for state, city in active_business_locations:
            if state and city:
                locations.append(
                    {"province_slug": slugify(state), "city_slug": slugify(city)}
                )

        for cat in categories:
            urls.add(f"/explore/category/{cat.key}")
            for subcat in cat.subcategories.all():
                urls.add(f"/explore/category/{cat.key}/{subcat.key}")

        for loc in locations:
            base_loc_path = f"/explore/{loc['province_slug']}/{loc['city_slug']}"
            urls.add(base_loc_path)

            for cat in categories:
                urls.add(f"{base_loc_path}/{cat.key}")
                for subcat in cat.subcategories.all():
                    urls.add(f"{base_loc_path}/{cat.key}/{subcat.key}")

        return list(urls)

    def location(self, item):
        return item
