# quickstart/views/public/user_support_views.py

from rest_framework import viewsets, status, mixins, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
from django.contrib.auth.models import Permission
from quickstart.models import SupportTicket, TicketMessage, CustomUser
from quickstart.utils.email_utils import (
    send_support_ticket_created_email,
    send_admin_user_reply_notification,
)
from quickstart.views.admin.support_management.support_ticket_views import (
    SimpleUserSerializer,
    TicketMessageSerializer,
)

import logging

logger = logging.getLogger(__name__)

# --- USER-FACING SERIALIZERS ---


class UserTicketListSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )

    class Meta:
        model = SupportTicket
        fields = (
            "ticket_id",
            "user_facing_id",
            "subject",
            "status",
            "status_display",
            "category",
            "category_display",
            "updated_at",
            "description",
        )


class UserTicketDetailSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    assigned_to_details = SimpleUserSerializer(source="assigned_to", read_only=True)
    user_details = SimpleUserSerializer(source="user", read_only=True)
    conversation = TicketMessageSerializer(many=True, read_only=True)

    class Meta:
        model = SupportTicket
        fields = (
            "ticket_id",
            "user_facing_id",
            "subject",
            "description",
            "status",
            "status_display",
            "priority",
            "priority_display",
            "category",
            "category_display",
            "created_at",
            "updated_at",
            "assigned_to_details",
            "user_details",
            "conversation",
            "resolution_notes",
        )


class TicketCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = ("subject", "category", "priority", "description")

    def create(self, validated_data):
        user = self.context["request"].user
        ticket = SupportTicket.objects.create(user=user, **validated_data)
        TicketMessage.objects.create(
            ticket=ticket,
            sender=user,
            sender_type="user",
            text=validated_data["description"],
        )
        return ticket


# --- USER-FACING VIEWSET ---


class UserSupportTicketViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SupportTicket.objects.filter(user=self.request.user).prefetch_related(
            "conversation__sender__role"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return TicketCreateSerializer
        if self.action == "list":
            return UserTicketListSerializer
        return UserTicketDetailSerializer

    def perform_create(self, serializer):
        """Override perform_create to add email notifications."""
        user = self.request.user
        # The serializer's create method handles TicketMessage creation
        ticket = serializer.save(user=user)

        try:
            # 1. Send confirmation to the user
            send_support_ticket_created_email(user=user, ticket=ticket)

            # 2. Notify the admin/support team (you will need a new template for this)
            # send_admin_new_ticket_notification(ticket=ticket)
            logger.info(
                f"Queued support ticket creation emails for ticket {ticket.ticket_id}."
            )
        except Exception as e:
            logger.error(
                f"Failed to send support ticket creation emails for ticket {ticket.ticket_id}: {e}",
                exc_info=True,
            )

    @action(detail=True, methods=["post"], url_path="reply")
    def reply(self, request, pk=None):
        ticket = self.get_object()
        message_text = request.data.get("message")
        if not message_text:
            return Response(
                {"error": "Message cannot be empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if ticket.status in ["resolved", "closed"]:
            ticket.status = "open"
            ticket.save(update_fields=["status"])

        new_message = TicketMessage.objects.create(
            ticket=ticket, sender=request.user, sender_type="user", text=message_text
        )

        # --- START: MODIFIED ADMIN NOTIFICATION EMAIL ---
        try:
            # Dynamically find users with permission to reply to support tickets.
            support_permission = Permission.objects.get(
                codename="reply_any_support_ticket"
            )
            admin_users = (
                CustomUser.objects.filter(
                    Q(groups__permissions=support_permission)
                    | Q(user_permissions=support_permission)
                    | Q(is_superuser=True),
                    is_active=True,
                )
                .distinct()
                .exclude(email__isnull=True)
                .exclude(email__exact="")
            )

            admin_recipients = list(admin_users.values_list("email", flat=True))

            if admin_recipients:
                # This is a new, dedicated email function.
                send_admin_user_reply_notification(
                    recipients=admin_recipients, ticket=ticket
                )
                logger.info(
                    f"Queued admin notification for user reply on ticket {ticket.ticket_id} to {len(admin_recipients)} recipients."
                )
            else:
                logger.warning(
                    f"No active admin recipients with 'reply_any_support_ticket' permission found for user reply on ticket {ticket.ticket_id}"
                )
        except Permission.DoesNotExist:
            logger.error(
                "Could not find the 'reply_any_support_ticket' permission. Please run the enhance_permissions command."
            )
        except Exception as e:
            logger.error(
                f"Failed to send admin notification for user reply on ticket {ticket.ticket_id}: {e}",
                exc_info=True,
            )
        # --- END: MODIFIED ADMIN NOTIFICATION EMAIL ---

        return Response(
            TicketMessageSerializer(new_message).data, status=status.HTTP_201_CREATED
        )
