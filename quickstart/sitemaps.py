# quickstart/sitemaps.py

import re

from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from django.utils.text import slugify
from django.utils import timezone
from django.db.models import Count, Max, Q
from urllib.parse import quote
from .models import ClassesMain, ClassCollection, BusinessInfo, BlogPost, BlogCategory


class StaticViewSitemap(Sitemap):
    """
    Sitemap for the main static pages of the site.
    """

    priority = 0.6
    changefreq = "monthly"

    def items(self):
        named = [
            "homepage",
            "business-welcome",
            "careers",
            "about-us",
            "terms-of-service",
            "privacy-policy",
            "blog",
        ]
        # Next.js routes not in Django url reverse()
        extra_paths = [
            "/about",
            "/giftcards",
            "/fees",
            "/cookie-policy",
            "/content-policy",
            "/copyright-policy",
        ]
        return [("named", n) for n in named] + [("path", p) for p in extra_paths]

    def location(self, item):
        kind, val = item
        if kind == "path":
            return val
        try:
            return reverse(val)
        except Exception:
            return f'/{val.replace("-welcome", "")}'

    def lastmod(self, obj):
        return timezone.now()


class ClassSitemap(Sitemap):
    """
    Sitemap for class pages. Only active classes with active, verified businesses.
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
    Sitemap for business pages. Only active, verified businesses.
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

        # 2. Collection Pages (SEO: /explore?collection=slug)
        # One aggregation query for all active collections (avoid N+1 per collection).
        active_collections = list(
            ClassCollection.objects.filter(is_active=True).order_by("sort_order")
        )
        coll_ids = [c.id for c in active_collections]
        slug_by_coll_id = {c.id: c.slug for c in active_collections}

        lastmod_by_collection_id = {}
        if coll_ids:
            lastmod_by_collection_id = {
                row["collections__id"]: row["latest"]
                for row in base_qs.filter(collections__id__in=coll_ids)
                .values("collections__id")
                .annotate(latest=Max("updatedAt"))
            }

        for coll in active_collections:
            coll_lastmod = lastmod_by_collection_id.get(coll.id) or main_lastmod
            urls.append(
                {
                    "url": f"/explore?collection={quote(coll.slug)}",
                    "lastmod": coll_lastmod,
                }
            )

        # 3. Location Pages
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

        # 5. Location + Collection Pages (SEO: location + collection)
        # Single grouped query for all active collections (avoid N+1 per collection).
        if coll_ids:
            loc_coll_rows = (
                base_qs.filter(collections__id__in=coll_ids)
                .exclude(businessId__businessCity__isnull=True)
                .exclude(businessId__businessCity="")
                .values(
                    "collections__id",
                    "businessId__businessCity",
                    "businessId__businessState",
                )
                .annotate(
                    last_updated=Max("updatedAt"),
                    lat=Max("businessId__latitude"),
                    lng=Max("businessId__longitude"),
                )
            )
            for item in loc_coll_rows:
                coll_slug = slug_by_coll_id.get(item["collections__id"])
                if not coll_slug:
                    continue
                city = item["businessId__businessCity"]
                state = item["businessId__businessState"] or ""
                lat = item["lat"]
                lng = item["lng"]
                if not city or not lat or not lng:
                    continue
                location_str = f"{city}, {state}" if state else city
                encoded_location = quote(location_str)
                urls.append(
                    {
                        "url": f"/explore?collection={quote(coll_slug)}&location={encoded_location}&lat={lat}&lng={lng}",
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


class BlogSitemap(Sitemap):
    """
    Sitemap for published blog posts only. Respects status=published.
    """

    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return (
            BlogPost.objects.filter(status="published")
            .select_related("category")
            .order_by("-updated_at")
        )

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return f"/blog/{obj.slug}"


class BlogCategorySitemap(Sitemap):
    """
    Sitemap for blog category archive pages (only categories that have published posts).
    """

    changefreq = "weekly"
    priority = 0.7

    def items(self):
        return (
            BlogCategory.objects.annotate(
                post_count=Count("posts", filter=Q(posts__status="published"))
            )
            .filter(post_count__gt=0)
            .order_by("name")
        )

    def lastmod(self, obj):
        return timezone.now()

    def location(self, obj):
        return f"/blog/category/{obj.slug}"


def _blog_tag_url_slug(tag):
    """Match Next.js blog tag URLs: tagToSlug in blog/tag/[tag]/page.jsx."""
    return re.sub(r"\s+", "-", str(tag).lower().strip())


class BlogTagSitemap(Sitemap):
    """
    Sitemap for blog tag archive pages (/blog/tag/{slug}/).
    """

    changefreq = "weekly"
    priority = 0.65

    def items(self):
        slugs = set()
        for post in BlogPost.objects.filter(status="published").only("tags"):
            for t in post.tags or []:
                if t is None or not str(t).strip():
                    continue
                slugs.add(_blog_tag_url_slug(t))
        return sorted(slugs)

    def location(self, tag_slug):
        return f"/blog/tag/{tag_slug}"

    def lastmod(self, tag_slug):
        latest = None
        for post in BlogPost.objects.filter(status="published").only(
            "tags", "updated_at"
        ):
            for t in post.tags or []:
                if t and _blog_tag_url_slug(t) == tag_slug:
                    if latest is None or post.updated_at > latest:
                        latest = post.updated_at
                    break
        return latest or timezone.now()