# quickstart/views/blog_views.py
import logging

from django.core.cache import cache
from rest_framework import viewsets, permissions, filters
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from django.db.models import Count, Q

from quickstart.models import BlogCategory, BlogPost
from quickstart.serializers import (
    PublicBlogPostListSerializer,
    PublicBlogPostDetailSerializer,
    PublicBlogCategorySerializer,
)

logger = logging.getLogger(__name__)

BLOG_POST_DETAIL_CACHE_PREFIX = "blog_post_detail"
BLOG_POST_DETAIL_CACHE_VERSION_PREFIX = "blog_post_detail_version"
BLOG_POST_DETAIL_CACHE_TTL = 60 * 60 * 6  # 6 hours


def invalidate_blog_post_detail_cache(slug):
    if not slug:
        return
    try:
        version_key = f"{BLOG_POST_DETAIL_CACHE_VERSION_PREFIX}:{slug}"
        version = cache.get(version_key, 0) or 0
        cache.set(version_key, version + 1, timeout=None)
        logger.info(
            "Invalidated blog post detail cache for slug=%s (version -> %s)",
            slug,
            version + 1,
        )
    except Exception as e:
        logger.warning(
            "Failed to invalidate blog post detail cache for slug=%s: %s", slug, e
        )


class BlogPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"


# --- Public Views ---


class PublicBlogPostViewSet(viewsets.ReadOnlyModelViewSet):
    """Public API for viewing blog posts."""

    permission_classes = [permissions.AllowAny]
    pagination_class = BlogPagination
    lookup_field = "slug"

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PublicBlogPostDetailSerializer
        return PublicBlogPostListSerializer

    def get_queryset(self):
        queryset = BlogPost.objects.filter(status="published").select_related(
            "author", "category"
        )

        # List responses never serialize `content`; defer avoids loading large HTML per row.
        if getattr(self, "action", None) == "list":
            queryset = queryset.defer("content")

        # Handle category filtering e.g. /api/blog/posts/?category=education-trends
        category_slug = self.request.query_params.get("category")
        if category_slug:
            queryset = queryset.filter(category__slug=category_slug)

        # Handle tag filtering e.g. /api/blog/posts/?tag=education (slug) or ?tag=Education (exact)
        tag_param = self.request.query_params.get("tag")
        if tag_param:
            # Support slug (e.g. "education") -> match tag "Education"; "some-tag" -> "Some Tag"
            tag_value = tag_param.replace("-", " ").title()
            queryset = queryset.filter(tags__contains=[tag_value])

        return queryset

    def retrieve(self, request, *args, **kwargs):
        slug = kwargs.get(self.lookup_field)
        version_key = f"{BLOG_POST_DETAIL_CACHE_VERSION_PREFIX}:{slug}"
        version = cache.get(version_key, 0) or 0
        cache_key = f"{BLOG_POST_DETAIL_CACHE_PREFIX}:{slug}:v{version}"
        data = cache.get(cache_key)
        if data is not None:
            return Response(data)

        response = super().retrieve(request, *args, **kwargs)
        try:
            cache.set(cache_key, response.data, timeout=BLOG_POST_DETAIL_CACHE_TTL)
        except Exception as e:
            logger.warning(
                "Failed to set blog post detail cache for slug=%s: %s", slug, e
            )
        return response


class PublicBlogCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Public API for viewing blog categories."""

    permission_classes = [permissions.AllowAny]
    serializer_class = PublicBlogCategorySerializer
    queryset = BlogCategory.objects.annotate(
        post_count=Count("posts", filter=Q(posts__status="published"))
    ).filter(post_count__gt=0)
    lookup_field = "slug"
