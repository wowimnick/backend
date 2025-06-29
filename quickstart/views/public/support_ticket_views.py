# quickstart/views/user/support_ticket_views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.views import APIView
from django.db.models import Count, Q
from django.utils import timezone
from django.db import transaction
import logging

from quickstart.utils.email_utils import send_support_ticket_created_email

from quickstart.models import SupportTicket, ChatMessage, ChatSession, CustomUser
from quickstart.serializers import (
    SupportTicketSerializer,
    ChatMessageSerializer,
    SupportTicketDetailSerializer,
    CreateSupportTicketSerializer,
    UserSupportTicketSerializer,
)

logger = logging.getLogger(__name__)


class IsTicketOwner(BasePermission):
    """Allows access only to the user who owns the ticket."""

    message = "You do not have permission to access this ticket."

    def has_object_permission(self, request, view, obj):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return obj.user == request.user


class UserSupportTicketViewSet(viewsets.ModelViewSet):
    """
    API endpoint for users to view and manage their OWN support tickets
    """

    serializer_class = SupportTicketSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["subject", "description"]
    ordering_fields = ["created_at", "updated_at", "status", "priority", "category"]
    ordering = ["-created_at"]
    http_method_names = ["get", "post", "head", "options"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return SupportTicketDetailSerializer
        return SupportTicketSerializer

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return SupportTicket.objects.none()
        queryset = (
            SupportTicket.objects.select_related("user", "assigned_to", "chat_session")
            .prefetch_related("chat_session__messages")
            .filter(user=user)
        )
        status_param = self.request.query_params.get("status")
        if status_param and status_param != "all":
            queryset = queryset.filter(status=status_param)
        category = self.request.query_params.get("category")
        if category and category != "all":
            queryset = queryset.filter(category=category)
        return queryset

    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.add_supportticket"):
            return Response(
                {"detail": "You do not have permission to create support tickets."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = CreateSupportTicketSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        try:
            ticket = serializer.save()
            logger.info(
                f"User {request.user.email} created Support Ticket {ticket.pk} via ViewSet"
            )
            try:
                send_support_ticket_created_email(request.user, ticket)
                logger.info(
                    f"Support ticket created confirmation email prepared/queued for ticket {ticket.pk}"
                )
            except Exception as email_error:
                logger.error(
                    f"Failed to send ticket created confirmation email for ticket {ticket.pk}: {email_error}",
                    exc_info=True,
                )
            response_serializer = UserSupportTicketSerializer(
                ticket, context={"request": request}
            )
            headers = self.get_success_headers(serializer.data)
            return Response(
                response_serializer.data,
                status=status.HTTP_201_CREATED,
                headers=headers,
            )
        except Exception as e:
            logger.error(
                f"Error creating support ticket for user {request.user.email} via ViewSet: {e}",
                exc_info=True,
            )
            return Response(
                {"detail": "An error occurred while creating the ticket."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def update(self, request, *args, **kwargs):
        return Response(
            {"detail": 'Method "PUT" not allowed.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def partial_update(self, request, *args, **kwargs):
        return Response(
            {"detail": 'Method "PATCH" not allowed.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        logger.warning(
            f"User {request.user.email} deleted their own Support Ticket {instance.pk} via ViewSet"
        )
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, IsTicketOwner],
    )
    def reply(self, request, pk=None):
        if not request.user.has_perm("quickstart.reply_own_support_ticket"):
            pass
        ticket = self.get_object()
        message_content = request.data.get("message", "").strip()
        if not message_content:
            return Response(
                {"error": "Message content is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if ticket.status == "closed":
            return Response(
                {"error": "Cannot reply to a closed ticket."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- FIX: ROBUST SESSION HANDLING ---
        # Always use the session associated with the ticket.
        # If one doesn't exist (data integrity issue), create and associate it.
        chat_session = ticket.chat_session
        if not chat_session:
            with transaction.atomic():
                logger.warning(f"Ticket {ticket.pk} had no chat_session. Creating one.")
                chat_session = ChatSession.objects.create(userId=ticket.user)
                ticket.chat_session = chat_session
                ticket.save(update_fields=["chat_session"])

        message = ChatMessage.objects.create(
            session=chat_session,  # Use the explicitly determined session
            content=message_content,
            is_user=True,
            sender_type="user",
        )

        if ticket.status == "resolved":
            ticket.status = "in_progress"
            ticket.save(update_fields=["status"])
            logger.info(f"Ticket {pk} reopened to 'in_progress' due to user reply.")
            ChatMessage.objects.create(
                session=chat_session,
                content="Ticket reopened by user reply.",
                is_user=False,
                sender_type="system",
            )
        logger.info(f"User {request.user.email} replied to Ticket {pk}")
        return Response(
            ChatMessageSerializer(message).data, status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        user = request.user
        if not user or not user.is_authenticated:
            return Response(
                {"error": "Authentication required."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        base_queryset = self.get_queryset()
        status_counts = base_queryset.values("status").annotate(count=Count("id"))
        result = {
            "total": base_queryset.count(),
            "counts": {item["status"]: item["count"] for item in status_counts},
        }
        return Response(result)


class CreateSupportTicketView(APIView):
    """
    (Functionality moved to UserSupportTicketViewSet.create, this is redundant)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.has_perm("quickstart.add_supportticket"):
            return Response(
                {"detail": "You do not have permission to create support tickets."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = CreateSupportTicketSerializer(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            try:
                ticket = serializer.save()
                logger.info(
                    f"User {request.user.email} created Support Ticket {ticket.pk} via dedicated View"
                )
                try:
                    send_support_ticket_created_email(request.user, ticket)
                    logger.info(
                        f"Support ticket created confirmation email prepared/queued for ticket {ticket.pk}"
                    )
                except Exception as email_error:
                    logger.error(
                        f"Failed to send ticket created confirmation email for ticket {ticket.pk}: {email_error}",
                        exc_info=True,
                    )
                response_serializer = UserSupportTicketSerializer(
                    ticket, context={"request": request}
                )
                return Response(
                    response_serializer.data, status=status.HTTP_201_CREATED
                )
            except Exception as e:
                logger.error(
                    f"Error creating support ticket for user {request.user.email} via dedicated View: {e}",
                    exc_info=True,
                )
                return Response(
                    {"detail": "An error occurred while creating the ticket."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
