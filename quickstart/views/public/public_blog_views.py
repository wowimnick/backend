# quickstart/views/blog_views.py
from rest_framework import viewsets, permissions, filters
from rest_framework.pagination import PageNumberPagination
from django.db.models import Count

from quickstart.serializers.admin.blog_management.admin_blog_serializers import (
    AdminBlogCategorySerializer,
    AdminBlogPostSerializer,
)
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

        # Handle category filtering e.g. /api/blog/posts/?category=education-trends
        category_slug = self.request.query_params.get("category")
        if category_slug:
            queryset = queryset.filter(category__slug=category_slug)

        return queryset


class PublicBlogCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Public API for viewing blog categories."""

    permission_classes = [permissions.AllowAny]
    serializer_class = PublicBlogCategorySerializer
    queryset = BlogCategory.objects.annotate(post_count=Count("posts")).filter(
        post_count__gt=0
    )
    lookup_field = "slug"


# --- Admin Views ---


class AdminBlogPostViewSet(viewsets.ModelViewSet):
    """Admin viewset for managing blog posts."""

    permission_classes = [
        permissions.IsAuthenticated
    ]  # Add 'quickstart.manage_blog_posts' later
    serializer_class = AdminBlogPostSerializer
    queryset = BlogPost.objects.all().select_related("author", "category")
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "title",
        "author__first_name",
        "author__last_name",
        "category__name",
    ]
    ordering_fields = ["published_date", "status", "title"]
    pagination_class = BlogPagination


class AdminBlogCategoryViewSet(viewsets.ModelViewSet):
    """Admin viewset for managing blog categories."""

    permission_classes = [
        permissions.IsAuthenticated
    ]  # Add 'quickstart.manage_blog_categories' later
    serializer_class = AdminBlogCategorySerializer
    queryset = BlogCategory.objects.annotate(post_count=Count("posts"))
