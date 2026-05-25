from rest_framework import viewsets, filters, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from django.db.models import Count
from django.db.models.deletion import ProtectedError

from quickstart.utils.permissions import IsAuthenticated, CanAccessBlogAdmin
from quickstart.utils.admin_pagination import AdminStandardPagination
from quickstart.utils.revalidation import trigger_nextjs_revalidation
from quickstart.serializers.admin.blog_management.admin_blog_serializers import (
    AdminBlogCategorySerializer,
    AdminBlogPostSerializer,
)
from quickstart.views.public.public_blog_views import BlogPagination
from quickstart.models import BlogCategory, BlogPost
import logging

logger = logging.getLogger(__name__)


# --- Admin Views ---
class AdminBlogPostViewSet(viewsets.ModelViewSet):
    """Admin viewset for managing blog posts."""

    permission_classes = [IsAuthenticated, CanAccessBlogAdmin]
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

    def _trigger_blog_revalidation(self, blog_post):
        """Helper to trigger revalidation for blog post changes."""
        if not blog_post:
            return

        # 1. Revalidate the specific blog post page
        if hasattr(blog_post, "slug") and blog_post.slug:
            trigger_nextjs_revalidation(tag=f"blog-post-{blog_post.slug}")
            logger.info(f"Revalidated blog post page: blog-post-{blog_post.slug}")

        # 2. Revalidate blog listing pages
        tags_to_revalidate = ["blog-posts", "blog-recent"]

        # 3. Revalidate category-specific pages if post has a category
        if blog_post.category and hasattr(blog_post.category, "slug"):
            tags_to_revalidate.append(f"category-{blog_post.category.slug}")

        for tag in tags_to_revalidate:
            trigger_nextjs_revalidation(tag=tag)
            logger.info(f"Revalidated tag: {tag}")

    def perform_create(self, serializer):
        """Create blog post and trigger revalidation."""
        blog_post = serializer.save()
        logger.info(
            f"Blog post '{blog_post.title}' created by admin {self.request.user.email}"
        )

        # Trigger revalidation after successful creation
        self._trigger_blog_revalidation(blog_post)

    def perform_update(self, serializer):
        """Update blog post and trigger revalidation."""
        blog_post = serializer.save()
        logger.info(
            f"Blog post '{blog_post.title}' updated by admin {self.request.user.email}"
        )

        # Trigger revalidation after successful update
        self._trigger_blog_revalidation(blog_post)

    def perform_destroy(self, instance):
        """Delete blog post and trigger revalidation."""
        blog_title = instance.title
        blog_slug = instance.slug
        blog_category_slug = instance.category.slug if instance.category else None

        # Delete the instance
        instance.delete()
        logger.info(
            f"Blog post '{blog_title}' deleted by admin {self.request.user.email}"
        )

        # Trigger revalidation after deletion
        trigger_nextjs_revalidation(tag=f"blog-post-{blog_slug}")
        trigger_nextjs_revalidation(tag="blog-posts")
        trigger_nextjs_revalidation(tag="blog-recent")

        if blog_category_slug:
            trigger_nextjs_revalidation(tag=f"category-{blog_category_slug}")

        logger.info(f"Revalidated blog pages after deletion of '{blog_title}'")


class AdminBlogCategoryViewSet(viewsets.ModelViewSet):
    """Admin viewset for managing blog categories."""

    permission_classes = [IsAuthenticated, CanAccessBlogAdmin]
    pagination_class = AdminStandardPagination
    serializer_class = AdminBlogCategorySerializer
    queryset = BlogCategory.objects.annotate(post_count=Count("posts"))

    def destroy(self, request, *args, **kwargs):
        """
        Prevent deletion of a category if it has posts assigned.
        Trigger revalidation if successful.
        """
        instance = self.get_object()
        category_name = instance.name
        category_slug = instance.slug if hasattr(instance, "slug") else None

        try:
            self.perform_destroy(instance)

            # Trigger revalidation after successful deletion
            trigger_nextjs_revalidation(tag="blog-categories")
            if category_slug:
                trigger_nextjs_revalidation(tag=f"category-{category_slug}")

            logger.info(f"Blog category '{category_name}' deleted and revalidated")
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ProtectedError:
            return Response(
                {
                    "error": "Cannot delete this category because it is associated with one or more blog posts. Please reassign the posts before deleting."
                },
                status=status.HTTP_409_CONFLICT,
            )
