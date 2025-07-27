from rest_framework import viewsets, permissions, filters, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from django.db.models import Count
from django.db.models.deletion import ProtectedError

from quickstart.serializers.admin.blog_management.admin_blog_serializers import (
    AdminBlogCategorySerializer,
    AdminBlogPostSerializer,
)
from quickstart.views.public.public_blog_views import BlogPagination
from quickstart.models import BlogCategory, BlogPost


class CanAccessBlogAdmin(permissions.BasePermission):
    """
    Allows access only to users with the 'access_blog_admin' permission.
    """

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("quickstart.access_blog_admin")
        )


# --- Admin Views ---
class AdminBlogPostViewSet(viewsets.ModelViewSet):
    """Admin viewset for managing blog posts."""

    permission_classes = [permissions.IsAuthenticated, CanAccessBlogAdmin]
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
        permissions.IsAuthenticated,
        CanAccessBlogAdmin,
    ]
    serializer_class = AdminBlogCategorySerializer
    queryset = BlogCategory.objects.annotate(post_count=Count("posts"))

    def destroy(self, request, *args, **kwargs):
        """
        Prevent deletion of a category if it has posts assigned.
        """
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ProtectedError:
            return Response(
                {
                    "error": "Cannot delete this category because it is associated with one or more blog posts. Please reassign the posts before deleting."
                },
                status=status.HTTP_409_CONFLICT,
            )
