"""
Public (booker) conversation ViewSet: list/create conversations, get detail with messages, send message.
"""

from django.db.models import Q
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError
from rest_framework.pagination import CursorPagination

from quickstart.models import (
    Conversation,
    ConversationMessage,
    BusinessInfo,
    Booking,
)
from quickstart.serializers.public.public_conversation_serializers import (
    ConversationListSerializer,
    ConversationDetailSerializer,
    ConversationCreateSerializer,
    ConversationMessageSerializer,
    ConversationMessageCreateSerializer,
)

import logging

logger = logging.getLogger(__name__)


def _user_can_start_conversation(user, business, booking=None):
    """Allow if user has a booking with this business or business allows public_with_chat."""
    if not user or not user.is_authenticated:
        return False
    if business.contact_privacy == "public_with_chat":
        return True
    qs = Booking.objects.filter(
        schedule_instance__schedule__option__classId__businessId=business
    ).exclude(status="cancelled")
    if booking and booking.user_id == user.pk:
        return qs.filter(user=user).exists()
    return qs.filter(user=user).exists()


def _get_conversation_for_booker(queryset, conversation_id, user):
    """Get conversation if current user is the booker (booker_user)."""
    conv = queryset.filter(id=conversation_id).first()
    if not conv:
        raise NotFound("Conversation not found.")
    if conv.booker_user_id != user.pk:
        raise PermissionDenied("You do not have access to this conversation.")
    return conv


class GuestConversationViewSet(viewsets.GenericViewSet):
    """
    Booker side: list my conversations, create (get-or-create), retrieve with messages, send message.
    """

    permission_classes = [IsAuthenticated]
    queryset = Conversation.objects.all()

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return Conversation.objects.none()
        return (
            Conversation.objects.filter(booker_user=user)
            .select_related("business", "booking", "booker_user", "booker_contact")
            .prefetch_related("messages__sender_user", "messages__sender_contact")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ConversationCreateSerializer
        if self.action == "retrieve":
            return ConversationDetailSerializer
        if self.action == "send_message":
            return ConversationMessageCreateSerializer
        return ConversationListSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset().order_by("-last_message_at", "-created_at")
        serializer = ConversationListSerializer(queryset, many=True)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        ser = ConversationCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        business_id = ser.validated_data["business_id"]
        booking_id = ser.validated_data.get("booking_id")
        user = request.user
        business = BusinessInfo.objects.filter(businessId=business_id).first()
        if not business:
            raise NotFound("Business not found.")
        if not _user_can_start_conversation(user, business):
            raise PermissionDenied(
                "You can only start a conversation with a business you have booked with, "
                "or when the business allows public chat."
            )
        booking = None
        if booking_id:
            booking = Booking.objects.filter(
                id=booking_id,
                user=user,
                schedule_instance__schedule__option__classId__businessId=business,
            ).first()
            if not booking:
                raise ValidationError(
                    {"booking_id": "Booking not found or not associated with this business."}
                )
        existing = Conversation.objects.filter(
            business=business, booker_user=user
        ).first()
        if existing:
            serializer = ConversationDetailSerializer(existing)
            return Response(serializer.data, status=status.HTTP_200_OK)
        conv = Conversation(
            business=business,
            booker_user=user,
            booker_contact=None,
            booking=booking,
        )
        conv.full_clean()
        conv.save()
        serializer = ConversationDetailSerializer(conv)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        conv = _get_conversation_for_booker(
            self.get_queryset(), kwargs["pk"], request.user
        )
        serializer = ConversationDetailSerializer(conv)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def send_message(self, request, pk=None):
        conv = _get_conversation_for_booker(
            self.get_queryset(), pk, request.user
        )
        ser = ConversationMessageCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        text = ser.validated_data["text"]
        msg = ConversationMessage.objects.create(
            conversation=conv,
            sender_type=ConversationMessage.SENDER_BOOKER,
            sender_user=request.user,
            sender_contact=None,
            text=text,
        )
        conv.last_message_at = timezone.now()
        conv.save(update_fields=["last_message_at"])
        from quickstart.utils.conversation_emails import (
            notify_business_new_message,
        )
        notify_business_new_message(conv, msg)
        from quickstart.utils.conversation_ws_broadcast import broadcast_new_message
        from quickstart.utils.notification_utils import create_notifications_for_new_chat_message
        broadcast_new_message(msg)
        create_notifications_for_new_chat_message(conv, msg)
        return Response(
            ConversationMessageSerializer(msg).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        """Mark conversation as read by the booker (updates last_read_by_booker_at)."""
        conv = _get_conversation_for_booker(
            self.get_queryset(), pk, request.user
        )
        conv.last_read_by_booker_at = timezone.now()
        conv.save(update_fields=["last_read_by_booker_at"])
        return Response({"last_read_by_booker_at": conv.last_read_by_booker_at})
