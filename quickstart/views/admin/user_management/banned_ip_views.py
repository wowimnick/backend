"""Admin API for IP ban management."""

from django.db import models
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.permissions import BasePermission

from quickstart.models import BannedIP
from quickstart.serializers.admin.user_management.banned_ip_serializers import (
    BannedIPSerializer,
)
from quickstart.utils.permissions import IsAuthenticated, CanAccessUserAdmin
from quickstart.utils.admin_pagination import AdminStandardPagination


class CanManageIPBans(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.has_perm("quickstart.manage_ip_bans")


class BannedIPViewSet(viewsets.ModelViewSet):
    """List, create, and delete banned IP addresses."""

    serializer_class = BannedIPSerializer
    permission_classes = [IsAuthenticated, CanAccessUserAdmin, CanManageIPBans]
    pagination_class = AdminStandardPagination
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = BannedIP.objects.select_related("created_by").order_by("-created_at")
        active_only = self.request.query_params.get("active")
        if active_only and active_only.lower() in ("1", "true", "yes"):
            now = timezone.now()
            qs = qs.filter(is_active=True).filter(
                models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
            )
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
