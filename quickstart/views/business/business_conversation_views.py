"""
Business dashboard conversation ViewSet: list conversations, get detail with messages, send reply.
"""

from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound

from quickstart.models import (
    Conversation,
    ConversationMessage,
    BusinessInfo,
    BusinessStaff,
)
from quickstart.serializers.business.business_conversation_serializers import (
    BusinessConversationListSerializer,
    BusinessConversationDetailSerializer,
    BusinessConversationMessageCreateSerializer,
    BusinessConversationMessageSerializer,
)
from quickstart.utils.permissions import IsBusinessOwnerOrManager

import logging

logger = logging.getLogger(__name__)


def _get_business_for_user(user):
    return (
        BusinessInfo.objects.filter(
            Q(owner=user) | Q(staff_members__user=user)
        )
        .distinct()
        .first()
    )


def _get_conversation_for_business(queryset, conversation_id, business):
    conv = queryset.filter(id=conversation_id).first()
    if not conv:
        raise NotFound("Conversation not found.")
    if conv.business_id != business.businessId:
        raise PermissionDenied("You do not have access to this conversation.")
    return conv


class BusinessConversationViewSet(viewsets.GenericViewSet):
    """
    Business side: list conversations for the business, retrieve with messages, send reply.
    """

    permission_classes = [IsAuthenticated, IsBusinessOwnerOrManager]

    def get_queryset(self):
        business = _get_business_for_user(self.request.user)
        if not business:
            return Conversation.objects.none()
        return (
            Conversation.objects.filter(business=business)
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

    def get_serializer_class(self):
        if self.action == "retrieve":
            return BusinessConversationDetailSerializer
        if self.action == "send_message":
            return BusinessConversationMessageCreateSerializer
        return BusinessConversationListSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = BusinessConversationListSerializer(queryset, many=True)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        business = _get_business_for_user(request.user)
        if not business:
            raise PermissionDenied("You are not associated with any business.")
        conv = _get_conversation_for_business(
            self.get_queryset(), kwargs["pk"], business
        )
        serializer = BusinessConversationDetailSerializer(conv)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def send_message(self, request, pk=None):
        business = _get_business_for_user(request.user)
        if not business:
            raise PermissionDenied("You are not associated with any business.")
        conv = _get_conversation_for_business(
            self.get_queryset(), pk, business
        )
        ser = BusinessConversationMessageCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        text = ser.validated_data["text"]
        msg = ConversationMessage.objects.create(
            conversation=conv,
            sender_type=ConversationMessage.SENDER_BUSINESS,
            sender_user=request.user,
            sender_contact=None,
            text=text,
        )
        conv.last_message_at = timezone.now()
        conv.save(update_fields=["last_message_at"])
        from quickstart.utils.conversation_emails import (
            notify_booker_new_reply,
        )
        notify_booker_new_reply(conv, msg)
        return Response(
            BusinessConversationMessageSerializer(msg).data,
            status=status.HTTP_201_CREATED,
        )
