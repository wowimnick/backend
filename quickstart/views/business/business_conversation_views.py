"""
Business dashboard conversation ViewSet: list conversations, get detail with messages, send reply.
"""

from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from quickstart.models import (
    Conversation,
    ConversationMessage,
    BusinessInfo,
    Booking,
)
from quickstart.serializers.business.business_conversation_serializers import (
    BusinessConversationListSerializer,
    BusinessConversationDetailSerializer,
    BusinessConversationMessageCreateSerializer,
    BusinessConversationMessageSerializer,
    BusinessStartConversationByBookingSerializer,
)
from quickstart.utils.conversation_moderation import (
    business_visible_conversations_queryset,
    business_visible_messages_queryset,
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
        visible_messages = business_visible_messages_queryset().select_related(
            "sender_user", "sender_contact"
        )
        return (
            business_visible_conversations_queryset()
            .filter(business=business)
            .select_related(
                "business",
                "booking",
                "booker_user",
                "booker_contact",
                "booking__schedule_instance__schedule__option__classId",
            )
            .prefetch_related(
                Prefetch("messages", queryset=visible_messages),
            )
            .order_by("-last_message_at", "-created_at")
        )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return BusinessConversationDetailSerializer
        if self.action == "send_message":
            return BusinessConversationMessageCreateSerializer
        if self.action == "start_by_booking":
            return BusinessStartConversationByBookingSerializer
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
        logger.info(
            "Business outbound message sent message_id=%s conversation_id=%s "
            "moderation_status=%s (business messages skip Gemini review)",
            msg.id,
            conv.id,
            msg.moderation_status,
        )
        conv.last_message_at = timezone.now()
        conv.save(update_fields=["last_message_at"])
        from quickstart.utils.conversation_emails import (
            notify_booker_new_reply,
        )
        from quickstart.utils.conversation_ws_broadcast import broadcast_new_message
        notify_booker_new_reply(conv, msg)
        broadcast_new_message(msg)
        return Response(
            BusinessConversationMessageSerializer(msg).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        """Mark conversation as read by the business (updates last_read_by_business_at)."""
        business = _get_business_for_user(request.user)
        if not business:
            raise PermissionDenied("You are not associated with any business.")
        conv = _get_conversation_for_business(
            self.get_queryset(), pk, business
        )
        conv.last_read_by_business_at = timezone.now()
        conv.save(update_fields=["last_read_by_business_at"])
        return Response({"last_read_by_business_at": conv.last_read_by_business_at})

    @action(detail=False, methods=["post"], url_path="start-by-booking")
    def start_by_booking(self, request):
        """
        Get or create a conversation for a booking owned by this business.
        Allows the business to message the guest first. Optional 'text' sends an initial message.
        """
        business = _get_business_for_user(request.user)
        if not business:
            raise PermissionDenied("You are not associated with any business.")

        ser = BusinessStartConversationByBookingSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        booking_id = ser.validated_data["booking_id"]
        first_message_text = (ser.validated_data.get("text") or "").strip()

        booking = (
            Booking.objects.filter(
                id=booking_id,
                schedule_instance__schedule__option__classId__businessId=business,
            )
            .select_related("user", "contact", "schedule_instance__schedule__option__classId")
            .first()
        )
        if not booking:
            raise NotFound("Booking not found or not associated with your business.")

        if booking.user_id and booking.contact_id:
            raise ValidationError(
                {"booking_id": "Booking has both user and contact; cannot determine booker."}
            )
        if not booking.user_id and not booking.contact_id:
            raise ValidationError(
                {"booking_id": "Booking has no guest (user or contact) to message."}
            )

        booker_user = booking.user if booking.user_id else None
        booker_contact = booking.contact if booking.contact_id else None

        conv = Conversation.objects.filter(
            business=business,
            booker_user=booker_user,
            booker_contact=booker_contact,
        ).select_related(
            "business",
            "booking",
            "booker_user",
            "booker_contact",
            "booking__schedule_instance__schedule__option__classId",
        ).prefetch_related("messages__sender_user", "messages__sender_contact").first()

        if conv:
            if not conv.booking_id:
                conv.booking = booking
                conv.save(update_fields=["booking_id"])
        else:
            conv = Conversation(
                business=business,
                booking=booking,
                booker_user=booker_user,
                booker_contact=booker_contact,
            )
            conv.full_clean()
            conv.save()

        if first_message_text:
            msg = ConversationMessage.objects.create(
                conversation=conv,
                sender_type=ConversationMessage.SENDER_BUSINESS,
                sender_user=request.user,
                sender_contact=None,
                text=first_message_text,
            )
            logger.info(
                "Business outbound message sent message_id=%s conversation_id=%s "
                "moderation_status=%s (business messages skip Gemini review)",
                msg.id,
                conv.id,
                msg.moderation_status,
            )
            conv.last_message_at = timezone.now()
            conv.save(update_fields=["last_message_at"])
            from quickstart.utils.conversation_emails import notify_booker_new_reply
            from quickstart.utils.conversation_ws_broadcast import broadcast_new_message
            notify_booker_new_reply(conv, msg)
            broadcast_new_message(msg)

        conv_serializer = BusinessConversationDetailSerializer(conv)
        return Response(conv_serializer.data, status=status.HTTP_200_OK)
