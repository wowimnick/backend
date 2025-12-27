from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.core.cache import cache  # For Redis
from django.db.models import Q

from quickstart.models import Notification, BusinessInfo  # Adjust import
from quickstart.serializers import NotificationSerializer  # Adjust import
# ADDED: Import Business Permissions
from quickstart.utils.permissions import IsBusinessMember


class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated, IsBusinessMember]
    http_method_names = ["get", "post", "head", "options"]  # Allow POST for actions

    def get_queryset(self):
        # Notifications for the current authenticated user OR for any business they manage/own
        user = self.request.user
        user_businesses_qs = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).distinct()
        business_ids = list(user_businesses_qs.values_list("pk", flat=True))

        return (
            Notification.objects.filter(Q(user=user) | Q(business_id__in=business_ids))
            .select_related("user", "business")
            .distinct()
            .order_by("-created_at")
        )

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        # Apply pagination if you have it configured for other viewsets
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        user = request.user
        cache_key_user = f"user:{user.pk}:unread_notifications_count"

        # Try to get from cache first
        count = cache.get(cache_key_user)

        if count is None:  # Cache miss
            # --- CORRECTED ---
            user_businesses_qs = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).distinct()
            business_ids = list(user_businesses_qs.values_list("pk", flat=True))

            count = (
                Notification.objects.filter(
                    (Q(user=user) | Q(business_id__in=business_ids)), is_read=False
                )
                .distinct()
                .count()
            )
            cache.set(cache_key_user, count, timeout=3600)  # Cache for 1 hour

        return Response({"unread_count": count})

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_as_read(self, request, pk=None):
        notification = self.get_object()
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=["is_read"])
            self._decrement_unread_count(notification.user, notification.business)
        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_as_read(self, request):
        user = request.user
        # --- CORRECTED ---
        user_businesses_qs = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).distinct()
        business_ids = list(user_businesses_qs.values_list("pk", flat=True))

        updated_count = (
            Notification.objects.filter(
                (Q(user=user) | Q(business_id__in=business_ids)), is_read=False
            )
            .distinct()
            .update(is_read=True)
        )

        if updated_count > 0:
            # Pass the list of business IDs to the clear function
            self._clear_unread_count(user, business_ids)

        return Response({"message": f"{updated_count} notifications marked as read."})

    # Helper methods for cache
    def _increment_unread_count(self, user, business=None):
        if user:
            cache_key_user = f"user:{user.pk}:unread_notifications_count"
            try:
                cache.incr(cache_key_user)
            except ValueError:
                cache.set(cache_key_user, 1, timeout=3600)

        # Also update for business context if notification is tied to a business and not user-specific
        if business:
            # We need to figure out which users manage this business to update their counts
            # This part gets tricky if notifications are for "a business" rather than "a specific user of a business".
            # For simplicity, we assume the notification is created with a specific user.
            # If a notification is for ALL managers of a business, the creation logic should create one for EACH manager.
            pass

    def _decrement_unread_count(self, user, business=None):
        if user:
            cache_key_user = f"user:{user.pk}:unread_notifications_count"
            try:
                current_val = cache.get(cache_key_user)
                if current_val is not None and current_val > 0:
                    cache.decr(cache_key_user)
                elif (
                    current_val is not None and current_val <= 0
                ):  # ensure it doesn't go negative from multiple calls
                    cache.set(cache_key_user, 0, timeout=3600)
            except ValueError:
                cache.set(cache_key_user, 0, timeout=3600)  # If key didn't exist

    def _clear_unread_count(self, user, business_ids_list=None):
        if user:
            cache_key_user = f"user:{user.pk}:unread_notifications_count"
            cache.set(cache_key_user, 0, timeout=3600)