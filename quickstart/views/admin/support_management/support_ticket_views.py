import logging
from rest_framework import viewsets, status, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Q, F, Avg
from django.contrib.auth import get_user_model
from quickstart.models import SupportTicket, TicketMessage, TicketHistoryLog
from rest_framework.pagination import PageNumberPagination
from quickstart.utils.permissions import IsAuthenticated, BasePermission, CanAccessSupportAdmin
from quickstart.utils.email_utils import send_agent_reply_email, send_ticket_resolved_email
from quickstart.views.admin.metrics_time_windows import get_admin_metrics_window

User = get_user_model()
logger = logging.getLogger(__name__)


# --- SERIALIZERS ---
class SimpleUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    avatar_thumb_url = serializers.ImageField(source="avatar", read_only=True)

    class Meta:
        model = User
        fields = ("userId", "full_name", "email", "avatar_thumb_url")

    def get_full_name(self, obj):
        return obj.get_full_name() or obj.email


class TicketMessageSerializer(serializers.ModelSerializer):
    sender_details = SimpleUserSerializer(
        source="sender", read_only=True, allow_null=True
    )

    class Meta:
        model = TicketMessage
        fields = ("id", "sender_type", "text", "timestamp", "sender_details")


class TicketHistoryLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketHistoryLog
        fields = ("id", "user_email", "details", "timestamp")


class AdminSupportTicketListSerializer(serializers.ModelSerializer):
    user_details = SimpleUserSerializer(source="user", read_only=True)
    assigned_to_details = SimpleUserSerializer(
        source="assigned_to", read_only=True, allow_null=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
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
            "priority",
            "priority_display",
            "category",
            "category_display",
            "created_at",
            "updated_at",
            "user_details",
            "assigned_to_details",
        )


class AdminSupportTicketDetailSerializer(AdminSupportTicketListSerializer):
    conversation = TicketMessageSerializer(many=True, read_only=True)
    user_context = serializers.SerializerMethodField()

    class Meta(AdminSupportTicketListSerializer.Meta):
        fields = AdminSupportTicketListSerializer.Meta.fields + (
            "description",
            "resolution_notes",
            "conversation",
            "user_context",
        )

    def get_user_context(self, obj):
        user = obj.user
        if not user:
            return None
        return {
            "member_since": user.createdAt.strftime("%b %Y"),
            "total_bookings": user.bookings.filter(status="completed").count(),
            "is_business_owner": hasattr(user, "owned_businesses")
            and user.owned_businesses.exists(),
        }


class ReplySerializer(serializers.Serializer):
    message = serializers.CharField(trim_whitespace=False, min_length=1)


class AssignTicketSerializer(serializers.Serializer):
    agent_id = serializers.IntegerField()

    def validate_agent_id(self, value):
        # FIX: Ensure the agent exists and has a support-related role
        try:
            agent = User.objects.get(
                pk=value,
                is_active=True,
                role__name__in=["Admin", "Super Admin", "Support Agent"],
            )
        except User.DoesNotExist:
            raise serializers.ValidationError(
                "An active agent with the specified ID does not exist."
            )
        return value


class ResolveTicketSerializer(serializers.Serializer):
    resolution_notes = serializers.CharField(min_length=10, max_length=5000)


# --- PAGINATION ---
class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


# --- VIEWSET ---


class AdminSupportTicketViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, CanAccessSupportAdmin]
    queryset = (
        SupportTicket.objects.select_related("user", "assigned_to")
        .prefetch_related("conversation__sender")
        .all()
    )
    pagination_class = StandardResultsSetPagination

    def get_serializer_class(self):
        return (
            AdminSupportTicketDetailSerializer
            if self.action == "retrieve"
            else AdminSupportTicketListSerializer
        )

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset().order_by("-updated_at")
        filters = request.query_params
        if filters.get("status") and filters["status"] != "all":
            queryset = queryset.filter(status=filters["status"])
        if filters.get("priority") and filters["priority"] != "all":
            queryset = queryset.filter(priority=filters["priority"])
        if filters.get("search"):
            search_term = filters["search"]
            queryset = queryset.filter(
                Q(subject__icontains=search_term)
                | Q(user_facing_id__icontains=search_term)
                | Q(user__email__icontains=search_term)
                | Q(user__first_name__icontains=search_term)
                | Q(user__last_name__icontains=search_term)
            )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], serializer_class=ReplySerializer)
    def reply(self, request, pk=None):
        ticket = self.get_object()
        serializer = ReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        TicketMessage.objects.create(
            ticket=ticket,
            sender=request.user,
            sender_type="agent",
            text=serializer.validated_data["message"],
        )
        TicketHistoryLog.objects.create(
            ticket=ticket,
            user=request.user,
            user_email=request.user.email,
            details="Replied to ticket.",
        )

        # FIX: Change status to 'in_progress' if it's 'open' or 'resolved'
        if ticket.status in ["open", "resolved"]:
            original_status = ticket.status
            ticket.status = "in_progress"
            ticket.save(update_fields=["status"])
            TicketHistoryLog.objects.create(
                ticket=ticket,
                user=request.user,
                user_email=request.user.email,
                details=f"Status changed from '{original_status.title()}' to 'In Progress' due to reply.",
            )

        # Notify ticket creator only when this reply is a direct response to the user
        # (previous message was from user), so we don't spam when agent sends multiple messages in a row
        previous_sender_types = (
            TicketMessage.objects.filter(ticket=ticket)
            .order_by("-timestamp")
            .values_list("sender_type", flat=True)[:2]
        )
        if len(previous_sender_types) >= 2 and previous_sender_types[1] == "user":
            try:
                send_agent_reply_email(
                    user=ticket.user,
                    ticket=ticket,
                    agent=request.user,
                )
            except Exception as e:
                logger.exception(
                    "Failed to send agent reply notification for ticket %s: %s",
                    ticket.ticket_id,
                    e,
                )

        return Response(AdminSupportTicketDetailSerializer(ticket).data)

    @action(detail=True, methods=["post"], serializer_class=AssignTicketSerializer)
    def assign(self, request, pk=None):
        if not request.user.has_perm("quickstart.assign_support_ticket"):
            return Response(
                {"detail": "You do not have permission to assign tickets."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ticket = self.get_object()
        serializer = AssignTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        agent = User.objects.get(pk=serializer.validated_data["agent_id"])

        old_agent_name = (
            ticket.assigned_to.get_full_name() if ticket.assigned_to else "Unassigned"
        )
        ticket.assigned_to = agent
        if ticket.status == "open":
            ticket.status = "in_progress"
        ticket.save()

        TicketHistoryLog.objects.create(
            ticket=ticket,
            user=request.user,
            user_email=request.user.email,
            details=f"Assigned ticket from {old_agent_name} to {agent.get_full_name()}.",
        )

        return Response(AdminSupportTicketDetailSerializer(ticket).data)

    @action(detail=True, methods=["post"], serializer_class=ResolveTicketSerializer)
    def resolve(self, request, pk=None):
        ticket = self.get_object()
        serializer = ResolveTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        ticket.status = "resolved"
        ticket.resolution_notes = serializer.validated_data["resolution_notes"]
        ticket.resolved_at = timezone.now()
        ticket.save()
        TicketHistoryLog.objects.create(
            ticket=ticket,
            user=request.user,
            user_email=request.user.email,
            details=f"Resolved ticket.",
        )

        try:
            send_ticket_resolved_email(user=ticket.user, ticket=ticket)
        except Exception as e:
            logger.exception(
                "Failed to send ticket resolved notification for ticket %s: %s",
                ticket.ticket_id,
                e,
            )

        return Response(AdminSupportTicketDetailSerializer(ticket).data)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        history_logs = self.get_object().history_logs.order_by("timestamp")
        return Response(TicketHistoryLogSerializer(history_logs, many=True).data)

    @action(detail=False, methods=["get"], url_path="assignable-agents")
    def get_assignable_agents(self, request):
        agents = User.objects.filter(
            is_active=True, role__name__in=["Admin", "Super Admin", "Support Agent"]
        ).distinct()
        return Response(SimpleUserSerializer(agents, many=True).data)

    @action(detail=False, methods=["get"])
    def stats(self, request):
        window = get_admin_metrics_window(
            request.query_params, default_days=30, logger=logger
        )
        tickets = SupportTicket.objects.filter(
            created_at__gte=window.start_dt,
            created_at__lt=window.end_dt_exclusive,
        )

        def format_timedelta(td):
            if not td:
                return "N/A"
            days, remainder = divmod(td.total_seconds(), 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, _ = divmod(remainder, 60)
            if days > 0:
                return f"{int(days)}d {int(hours)}h"
            return f"{int(hours)}h {int(minutes)}m" if hours > 0 else f"{int(minutes)}m"

        return Response(
            {
                "open_tickets": tickets.filter(status="open").count(),
                "in_progress_tickets": tickets.filter(status="in_progress").count(),
                "avg_first_response_time": format_timedelta(
                    tickets.filter(first_agent_response_at__isnull=False).aggregate(
                        avg=Avg(F("first_agent_response_at") - F("created_at"))
                    )["avg"]
                ),
                "avg_resolution_time": format_timedelta(
                    tickets.filter(
                        status__in=["resolved", "closed"], resolved_at__isnull=False
                    ).aggregate(avg=Avg(F("resolved_at") - F("created_at")))["avg"]
                ),
            }
        )
