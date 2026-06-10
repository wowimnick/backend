"""
Admin: read-only list and detail of all guest–business conversations for management.
"""

from django.db.models import Prefetch
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from quickstart.models import BannedIP, Conversation, ConversationMessage
from quickstart.serializers.business.business_conversation_serializers import (
    BusinessConversationListSerializer,
    BusinessConversationDetailSerializer,
    BusinessConversationMessageSerializer,
)
from quickstart.utils.conversation_delivery import deliver_booker_message
from quickstart.utils.permissions import IsAuthenticated, CanAccessSupportAdmin
from quickstart.utils.admin_pagination import AdminStandardPagination
class AdminConversationMessageSerializer(BusinessConversationMessageSerializer):
    class Meta(BusinessConversationMessageSerializer.Meta):
        fields = list(BusinessConversationMessageSerializer.Meta.fields) + [
            "moderation_status",
            "moderation_reason",
            "moderation_confidence",
            "moderated_at",
            "sender_ip",
        ]
        read_only_fields = fields


class AdminConversationDetailSerializer(BusinessConversationDetailSerializer):
    messages = AdminConversationMessageSerializer(many=True, read_only=True)


class AdminConversationListSerializer(BusinessConversationListSerializer):
    """Admin list shows latest message regardless of moderation status."""

    def get_last_message_preview(self, obj):
        prefetched = getattr(obj, "_prefetched_objects_cache", {}).get("messages")
        if prefetched is not None:
            last = prefetched[0] if prefetched else None
        else:
            last = obj.messages.order_by("-created_at").first()
        if not last:
            return None
        text = (last.text or "").strip()
        return (text[:100] + "…") if len(text) > 100 else text if text else None


class AdminConversationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List and retrieve all conversations (read-only).
    Optional filters: business_id, moderation_status.
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

        moderation_status = self.request.query_params.get("moderation_status")
        if moderation_status:
            qs = qs.filter(messages__moderation_status=moderation_status).distinct()

        if getattr(self, "action", None) == "list":
            qs = qs.prefetch_related(
                Prefetch(
                    "messages",
                    queryset=ConversationMessage.objects.select_related(
                        "sender_user", "sender_contact"
                    ).order_by("-created_at")[:1],
                )
            )
        else:
            qs = qs.prefetch_related(
                "messages__sender_user", "messages__sender_contact"
            )
        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AdminConversationDetailSerializer
        return AdminConversationListSerializer

    @action(detail=False, methods=["post"], url_path="messages/approve")
    def approve_message(self, request):
        return self._moderate_message(request, approve=True)

    @action(detail=False, methods=["post"], url_path="messages/reject")
    def reject_message(self, request):
        return self._moderate_message(request, approve=False)

    def _moderate_message(self, request, *, approve: bool):
        message_id = request.data.get("message_id")
        if not message_id:
            return Response(
                {"detail": "message_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        msg = (
            ConversationMessage.objects.select_related("conversation", "conversation__business")
            .filter(pk=message_id, sender_type=ConversationMessage.SENDER_BOOKER)
            .first()
        )
        if not msg:
            return Response(
                {"detail": "Message not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        now = timezone.now()
        if approve:
            if msg.moderation_status == ConversationMessage.MODERATION_APPROVED:
                return Response({"status": "already_approved"})
            was_quarantined = msg.moderation_status in (
                ConversationMessage.MODERATION_PENDING,
                ConversationMessage.MODERATION_REJECTED,
            )
            msg.moderation_status = ConversationMessage.MODERATION_APPROVED
            msg.moderated_at = now
            msg.save(update_fields=["moderation_status", "moderated_at"])
            if was_quarantined:
                deliver_booker_message(msg.conversation, msg)
            return Response({"status": "approved"})

        msg.moderation_status = ConversationMessage.MODERATION_REJECTED
        msg.moderated_at = now
        msg.save(update_fields=["moderation_status", "moderated_at"])

        if request.data.get("ban_ip") and msg.sender_ip:
            BannedIP.objects.get_or_create(
                ip_address=msg.sender_ip,
                defaults={
                    "reason": f"Rejected scam message {msg.id}",
                    "created_by": request.user,
                    "is_active": True,
                },
            )

        return Response({"status": "rejected"})
