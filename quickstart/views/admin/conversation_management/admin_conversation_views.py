"""
Admin: read-only list and detail of all guest–business conversations for management.
"""

from rest_framework import viewsets
from rest_framework.response import Response
from django.db.models import Q

from quickstart.models import Conversation
from quickstart.serializers.business.business_conversation_serializers import (
    BusinessConversationListSerializer,
    BusinessConversationDetailSerializer,
)
from quickstart.utils.permissions import IsAuthenticated, CanAccessSupportAdmin


class AdminConversationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List and retrieve all conversations (read-only). Optional filter: business_id.
    """

    permission_classes = [IsAuthenticated, CanAccessSupportAdmin]
    queryset = (
        Conversation.objects.all()
        .select_related(
            "business",
            "booking",
            "booker_user",
            "booker_contact",
            "booking__schedule_instance__schedule__option__classId",
        )
        .prefetch_related("messages__sender_user", "messages__sender_contact")
        .order_by("-last_message_at", "-created_at")
    )

    def get_queryset(self):
        qs = super().get_queryset()
        business_id = self.request.query_params.get("business_id")
        if business_id:
            qs = qs.filter(business_id=business_id)
        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return BusinessConversationDetailSerializer
        return BusinessConversationListSerializer
