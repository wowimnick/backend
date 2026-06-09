"""
Admin: read-only list and detail of all guest–business conversations for management.
"""

from django.db.models import Prefetch
from rest_framework import viewsets
from rest_framework.response import Response

from quickstart.models import Conversation, ConversationMessage
from quickstart.serializers.business.business_conversation_serializers import (
    BusinessConversationListSerializer,
    BusinessConversationDetailSerializer,
)
from quickstart.utils.permissions import IsAuthenticated, CanAccessSupportAdmin
from quickstart.utils.admin_pagination import AdminStandardPagination


class AdminConversationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List and retrieve all conversations (read-only). Optional filter: business_id.
    """

    permission_classes = [IsAuthenticated, CanAccessSupportAdmin]
    pagination_class = AdminStandardPagination

    def get_queryset(self):
        qs = (
            Conversation.objects.all()
            .select_related(
                "business",
                "booking",
                "booker_user",
                "booker_contact",
                "booking__schedule_instance__schedule__option__classId",
            )
            .order_by("-last_message_at", "-created_at")
        )
        business_id = self.request.query_params.get("business_id")
        if business_id:
            qs = qs.filter(business_id=business_id)

        if getattr(self, "action", None) == "list":
            qs = qs.prefetch_related(
                Prefetch(
                    "messages",
                    queryset=ConversationMessage.objects.select_related(
                        "sender_user", "sender_contact"
                    ).order_by("-created_at"),
                )
            )
        else:
            qs = qs.prefetch_related(
                "messages__sender_user", "messages__sender_contact"
            )
        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return BusinessConversationDetailSerializer
        return BusinessConversationListSerializer
