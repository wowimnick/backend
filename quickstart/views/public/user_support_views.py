# In: quickstart/views/user_support_views.py
# Action: Create this new file.

from rest_framework import viewsets, status, mixins, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from quickstart.models import SupportTicket, TicketMessage
from quickstart.views.admin.support_management.support_ticket_views import (
    SimpleUserSerializer,
    TicketMessageSerializer,
)

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

        # The frontend expects the new message object in response
        return Response(
            TicketMessageSerializer(new_message).data, status=status.HTTP_201_CREATED
        )
