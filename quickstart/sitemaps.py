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

    URL Pattern: /explore?category={key}&subcategory={key}&location={city, state}&lat={lat}&lng={lng}

    All parameters are properly URL-encoded.
    Categories and subcategories are ONLY in query parameters, never in the path.
    """

    changefreq = "daily"
    priority = 0.8

    def items(self):
        """
        Generate explore page URLs with last modified dates.
        Returns list of dicts: {'url': str, 'lastmod': datetime}
        """
        urls = []

        # 1. Main explore page (no filters)
        latest_class = (
            ClassesMain.objects.filter(
                status="active",
                businessId__isActive=True,
                businessId__verificationStatus="verified",
            )
            .order_by("-updatedAt")
            .first()
        )

        main_lastmod = latest_class.updatedAt if latest_class else timezone.now()
        urls.append({"url": "/explore", "lastmod": main_lastmod})

        # Get all active categories
        categories = ClassCategory.objects.all()

        # 2. Category pages (no location)
        # e.g., /explore?category=fitness
        for cat in categories:
            cat_classes = ClassesMain.objects.filter(
                status="active",
                businessId__isActive=True,
                businessId__verificationStatus="verified",
                category=cat,
            )

            if cat_classes.exists():
                cat_latest = cat_classes.order_by("-updatedAt").first()
                urls.append(
                    {
                        "url": f"/explore?category={cat.key}",
                        "lastmod": (
                            cat_latest.updatedAt if cat_latest else timezone.now()
                        ),
                    }
                )

                # 2b. Subcategory pages (no location)
                # e.g., /explore?category=fitness&subcategory=yoga
                for subcat in cat.subcategories.all():
                    subcat_classes = ClassesMain.objects.filter(
                        status="active",
                        businessId__isActive=True,
                        businessId__verificationStatus="verified",
                        category=cat,
                        subcategory=subcat,
                    )

                    if subcat_classes.exists():
                        subcat_latest = subcat_classes.order_by("-updatedAt").first()
                        urls.append(
                            {
                                "url": f"/explore?category={cat.key}&subcategory={subcat.key}",
                                "lastmod": (
                                    subcat_latest.updatedAt
                                    if subcat_latest
                                    else timezone.now()
                                ),
                            }
                        )

        # 3. Get active locations with coordinates from businesses
        # Group by city/state to get unique location combinations
        active_locations = (
            BusinessInfo.objects.filter(
                isActive=True,
                verificationStatus="verified",
                latitude__isnull=False,
                longitude__isnull=False,
            )
            .exclude(businessState__isnull=True)
            .exclude(businessCity__isnull=True)
            .exclude(businessState="")
            .exclude(businessCity="")
            .values("businessState", "businessCity", "latitude", "longitude")
            .annotate(
                class_count=Count("classes", filter=Q(classes__status="active")),
                last_updated=Max(
                    "classes__updatedAt", filter=Q(classes__status="active")
                ),
            )
            .filter(class_count__gt=0)
        )

        # Create location URLs with proper URL encoding
        for loc in active_locations:
            city = loc["businessCity"]
            state = loc["businessState"]
            lat = float(loc["latitude"])
            lng = float(loc["longitude"])
            last_updated = loc["last_updated"] or timezone.now()

            # Format location string as "City, State" and URL encode it
            location_str = f"{city}, {state}"
            encoded_location = quote(location_str)

            # 4. Location-only pages - properly URL encoded
            # e.g., /explore?location=Toronto%2C%20ON&lat=43.65&lng=-79.38
            location_url = f"/explore?location={encoded_location}&lat={lat}&lng={lng}"
            urls.append({"url": location_url, "lastmod": last_updated})

            # 5. Location + Category combinations
            for cat in categories:
                cat_loc_classes = ClassesMain.objects.filter(
                    status="active",
                    businessId__isActive=True,
                    businessId__verificationStatus="verified",
                    businessId__businessCity__iexact=city,
                    businessId__businessState__iexact=state,
                    category=cat,
                )

                if cat_loc_classes.exists():
                    cat_loc_latest = cat_loc_classes.order_by("-updatedAt").first()
                    cat_loc_url = f"/explore?category={cat.key}&location={encoded_location}&lat={lat}&lng={lng}"
                    urls.append(
                        {
                            "url": cat_loc_url,
                            "lastmod": (
                                cat_loc_latest.updatedAt
                                if cat_loc_latest
                                else timezone.now()
                            ),
                        }
                    )

                    # 6. Location + Category + Subcategory combinations
                    for subcat in cat.subcategories.all():
                        subcat_loc_classes = ClassesMain.objects.filter(
                            status="active",
                            businessId__isActive=True,
                            businessId__verificationStatus="verified",
                            businessId__businessCity__iexact=city,
                            businessId__businessState__iexact=state,
                            category=cat,
                            subcategory=subcat,
                        )

                        if subcat_loc_classes.exists():
                            subcat_loc_latest = subcat_loc_classes.order_by(
                                "-updatedAt"
                            ).first()
                            subcat_loc_url = f"/explore?category={cat.key}&subcategory={subcat.key}&location={encoded_location}&lat={lat}&lng={lng}"
                            urls.append(
                                {
                                    "url": subcat_loc_url,
                                    "lastmod": (
                                        subcat_loc_latest.updatedAt
                                        if subcat_loc_latest
                                        else timezone.now()
                                    ),
                                }
                            )

        return urls

    def location(self, item):
        """Extract URL from item dict - already properly encoded"""
        return item["url"]

    def lastmod(self, item):
        """Extract lastmod from item dict"""
        return item["lastmod"]
