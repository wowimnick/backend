# quickstart/sitemaps.py

from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from django.utils.text import slugify
from django.utils import timezone
from django.db.models import Count, Max, Q
from urllib.parse import quote
from .models import ClassesMain, ClassCategory, ClassSubcategory, BusinessInfo


class StaticViewSitemap(Sitemap):
    """
    Sitemap for the main static pages of the site.
    """

    priority = 0.6
    changefreq = "monthly"

    def items(self):
        return [
            "homepage",
            "business-welcome",
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

    def lastmod(self, obj):
        return timezone.now()


class ClassSitemap(Sitemap):
    """
    Sitemap for all individual, publicly accessible class pages.
    """

    changefreq = "weekly"
    priority = 0.9

    def items(self):
        return (
            ClassesMain.objects.filter(
                status="active",
                businessId__isActive=True,
                businessId__verificationStatus="verified",
            )
            .select_related("businessId")
            .order_by("-updatedAt")
        )

    def lastmod(self, obj):
        return obj.updatedAt

    def location(self, obj):
        return f"/classes/{obj.slug}"


class BusinessSitemap(Sitemap):
    """
    Sitemap for all individual, publicly accessible business pages.
    """

    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return BusinessInfo.objects.filter(
            isActive=True,
            verificationStatus="verified",
        ).order_by("-updatedAt")

    def lastmod(self, obj):
        return obj.updatedAt

    def location(self, obj):
        return f"/business/{obj.slug}"


class ExplorePagesSitemap(Sitemap):
    """
    Dynamically generates sitemap entries for key explore pages using ONLY query parameters.
    OPTIMIZED: Uses aggregation to avoid N+1 queries.
    """

    changefreq = "daily"
    priority = 0.8

    def items(self):
        """
        Generate explore page URLs with last modified dates using aggregation
        to fetch valid combinations in minimal database queries.
        """
        urls = []

        # Base queryset for all active, verified classes
        # We reuse this to ensure consistency across all sitemap entries
        base_qs = ClassesMain.objects.filter(
            status="active",
            businessId__isActive=True,
            businessId__verificationStatus="verified",
        )

        # 1. Main explore page (no filters)
        # Fetch the most recent update time across all active classes
        main_agg = base_qs.aggregate(latest=Max("updatedAt"))
        main_lastmod = main_agg["latest"] or timezone.now()
        urls.append({"url": "/explore", "lastmod": main_lastmod})

        # 2. Category Pages
        # Group by category key and get the max updatedAt for each
        cat_items = base_qs.values("category__key").annotate(
            last_updated=Max("updatedAt")
        )

        for item in cat_items:
            urls.append(
                {
                    "url": f"/explore?category={item['category__key']}",
                    "lastmod": item["last_updated"],
                }
            )

        # 3. Category + Subcategory Pages
        # Group by category AND subcategory
        subcat_items = (
            base_qs.exclude(subcategory__isnull=True)
            .values("category__key", "subcategory__key")
            .annotate(last_updated=Max("updatedAt"))
        )

        for item in subcat_items:
            urls.append(
                {
                    "url": f"/explore?category={item['category__key']}&subcategory={item['subcategory__key']}",
                    "lastmod": item["last_updated"],
                }
            )

        # 4. Location Pages
        # Group by City and State. We assume one lat/lng pair per city is sufficient for the sitemap.
        # We fetch the Max latitude/longitude to ensure we get a valid coordinate pair for the city.
        loc_qs = (
            base_qs.exclude(businessId__businessCity__isnull=True)
            .exclude(businessId__businessCity="")
            .values("businessId__businessCity", "businessId__businessState")
            .annotate(
                last_updated=Max("updatedAt"),
                lat=Max("businessId__latitude"),
                lng=Max("businessId__longitude"),
            )
        )

        for item in loc_qs:
            city = item["businessId__businessCity"]
            state = item["businessId__businessState"] or ""
            lat = item["lat"]
            lng = item["lng"]

            # Skip if we don't have coordinates or city
            if not city or not lat or not lng:
                continue

            location_str = f"{city}, {state}" if state else city
            encoded_location = quote(location_str)

            urls.append(
                {
                    "url": f"/explore?location={encoded_location}&lat={lat}&lng={lng}",
                    "lastmod": item["last_updated"],
                }
            )

        # 5. Location + Category Pages
        # Group by City, State, AND Category
        loc_cat_qs = (
            base_qs.exclude(businessId__businessCity__isnull=True)
            .exclude(businessId__businessCity="")
            .values(
                "businessId__businessCity",
                "businessId__businessState",
                "category__key",
            )
            .annotate(
                last_updated=Max("updatedAt"),
                lat=Max("businessId__latitude"),
                lng=Max("businessId__longitude"),
            )
        )

        for item in loc_cat_qs:
            city = item["businessId__businessCity"]
            state = item["businessId__businessState"] or ""
            lat = item["lat"]
            lng = item["lng"]
            cat_key = item["category__key"]

            if not city or not lat or not lng:
                continue

            location_str = f"{city}, {state}" if state else city
            encoded_location = quote(location_str)

            urls.append(
                {
                    "url": f"/explore?category={cat_key}&location={encoded_location}&lat={lat}&lng={lng}",
                    "lastmod": item["last_updated"],
                }
            )

        # 6. Location + Category + Subcategory Pages
        # Group by City, State, Category AND Subcategory
        loc_subcat_qs = (
            base_qs.exclude(businessId__businessCity__isnull=True)
            .exclude(businessId__businessCity="")
            .exclude(subcategory__isnull=True)
            .values(
                "businessId__businessCity",
                "businessId__businessState",
                "category__key",
                "subcategory__key",
            )
            .annotate(
                last_updated=Max("updatedAt"),
                lat=Max("businessId__latitude"),
                lng=Max("businessId__longitude"),
            )
        )

        for item in loc_subcat_qs:
            city = item["businessId__businessCity"]
            state = item["businessId__businessState"] or ""
            lat = item["lat"]
            lng = item["lng"]
            cat_key = item["category__key"]
            subcat_key = item["subcategory__key"]

            if not city or not lat or not lng:
                continue

            location_str = f"{city}, {state}" if state else city
            encoded_location = quote(location_str)

            urls.append(
                {
                    "url": f"/explore?category={cat_key}&subcategory={subcat_key}&location={encoded_location}&lat={lat}&lng={lng}",
                    "lastmod": item["last_updated"],
                }
            )

        return urls

    def location(self, item):
        """Extract URL from item dict"""
        return item["url"]

    def lastmod(self, item):
        """Extract lastmod from item dict"""
        return item["lastmod"]