# quickstart/views/blog_views.py
from rest_framework import viewsets, permissions, filters
from rest_framework.pagination import PageNumberPagination
from django.db.models import Count, Q

from quickstart.models import BlogCategory, BlogPost
from quickstart.serializers import (
    PublicBlogPostListSerializer,
    PublicBlogPostDetailSerializer,
    PublicBlogCategorySerializer,
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


class PublicBlogCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Public API for viewing blog categories."""

    permission_classes = [permissions.AllowAny]
    serializer_class = PublicBlogCategorySerializer
    queryset = BlogCategory.objects.annotate(
        post_count=Count("posts", filter=Q(posts__status="published"))
    ).filter(post_count__gt=0)
    lookup_field = "slug"
