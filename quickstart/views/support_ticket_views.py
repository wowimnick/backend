from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.views import APIView
from django.db.models import Count, Q # Added Q
from django.utils import timezone # Added timezone
import logging

# Assuming models are in the parent app directory structure
from ..models import SupportTicket, ChatMessage, ChatSession, CustomUser
# Assuming serializers are structured similarly
from ..serializers import ( # Example path
    SupportTicketSerializer, ChatMessageSerializer, SupportTicketDetailSerializer,
    CreateSupportTicketSerializer, UserSupportTicketSerializer # UserSupportTicketSerializer might be same as SupportTicketSerializer
)

logger = logging.getLogger(__name__)

# --- Custom Permission Class (for object-level checks if needed) ---
class IsTicketOwner(BasePermission):
    """ Allows access only to the user who owns the ticket. """
    message = "You do not have permission to access this ticket."
    def has_object_permission(self, request, view, obj):
        # obj is the SupportTicket instance
        if not request.user or not request.user.is_authenticated or not request.user.is_active:
             return False
        # Check if the request user is the owner of the ticket
        return obj.user == request.user

# --- UserSupportTicketViewSet ---
class UserSupportTicketViewSet(viewsets.ModelViewSet):
    """
    API endpoint for users to view and manage their OWN support tickets
    """
    serializer_class = SupportTicketSerializer # Base serializer for list/update
    permission_classes = [IsAuthenticated] # Base permission: user must be logged in
    filter_backends = [filters.SearchFilter, filters.OrderingFilter] # Add ordering
    search_fields = ['subject', 'description'] # Allow searching own tickets
    ordering_fields = ['created_at', 'updated_at', 'status', 'priority', 'category']
    ordering = ['-created_at'] # Default order

    # Restrict HTTP methods allowed for users
    http_method_names = ['get', 'post', 'head', 'options'] # Allow GET (list, retrieve), POST (reply, potentially create)

    def get_serializer_class(self):
        # Use detail serializer for retrieve action
        if self.action == 'retrieve':
            return SupportTicketDetailSerializer
        # Use specific serializer for create if handled here
        # if self.action == 'create': # Typically handled by CreateSupportTicketView now
        #     return CreateSupportTicketSerializer
        return SupportTicketSerializer # Default for list

    def get_queryset(self):
        """
        Return only tickets belonging to the current authenticated user.
        """
        user = self.request.user
        if not user or not user.is_authenticated:
            return SupportTicket.objects.none() # Return empty if not authenticated

        # Base query filtered by user, with optimizations
        queryset = SupportTicket.objects.select_related(
            'user', 'assigned_to', 'chat_session' # Still select related for display
        ).prefetch_related(
            'chat_session__messages' # Prefetch messages for detail view efficiency
        ).filter(user=user)

        # --- Filtering Logic for User's Tickets ---
        status_param = self.request.query_params.get('status')
        if status_param and status_param != 'all':
            queryset = queryset.filter(status=status_param)

        category = self.request.query_params.get('category')
        if category and category != 'all':
            queryset = queryset.filter(category=category)

        # Search and Ordering handled by filter backends

        return queryset

    # Override standard methods to ensure ownership or apply specific permissions

    def list(self, request, *args, **kwargs):
        # Basic auth already checked, get_queryset filters by owner
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        # Use IsTicketOwner permission to ensure user owns this specific ticket
        # This check happens automatically if added to permission_classes,
        # but we can be explicit or add it just for this action if needed.
        instance = self.get_object() # get_object applies queryset filtering first
        # Optionally, add extra check if IsTicketOwner isn't globally applied
        # if instance.user != request.user:
        #    self.permission_denied(request, message="You do not own this ticket.")
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    # Block standard create/update/delete if handled elsewhere or not allowed
    def create(self, request, *args, **kwargs):
        # Usually handled by CreateSupportTicketView, block here
        return Response({"detail": "Method \"POST\" not allowed."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def update(self, request, *args, **kwargs):
        # Users typically shouldn't update tickets directly, only reply
        return Response({"detail": "Method \"PUT\" not allowed."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def partial_update(self, request, *args, **kwargs):
        # Users typically shouldn't update tickets directly, only reply
        return Response({"detail": "Method \"PATCH\" not allowed."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, *args, **kwargs):
        # Decide if users can delete their own tickets
        # return Response({"detail": "Method \"DELETE\" not allowed."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)
        # OR implement with IsTicketOwner check:
        instance = self.get_object() # Verifies ownership via get_queryset filter
        logger.warning(f"User {request.user.email} deleted their own Support Ticket {instance.pk}")
        # Add AuditLog?
        return super().destroy(request, *args, **kwargs)


    # --- Custom Actions for Users ---

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, IsTicketOwner]) # Ensure owner
    def reply(self, request, pk=None):
        """Add a user reply to their own support ticket"""
        # Check specific permission if defined, otherwise rely on IsTicketOwner
        if not request.user.has_perm('quickstart.reply_own_support_ticket'):
             # Fallback check if permission exists but wasn't assigned (should not happen if assigned correctly)
             # self.permission_denied(request, message="You do not have permission to reply to tickets.")
             # For now, rely on IsTicketOwner decorator check
             pass


        ticket = self.get_object() # Ownership checked by decorator/get_object
        message_content = request.data.get('message', '').strip()

        if not message_content:
            return Response({'error': 'Message content is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Prevent replying to closed tickets? Optional rule.
        # if ticket.status == 'closed':
        #    return Response({'error': 'Cannot reply to a closed ticket.'}, status=400)

        # Get or create chat session
        with transaction.atomic(): # Use atomic transaction
             chat_session, created = ChatSession.objects.get_or_create(userId=request.user) # Use request.user
             if not ticket.chat_session:
                 ticket.chat_session = chat_session
                 ticket.save(update_fields=['chat_session'])

        # Create user message
        message = ChatMessage.objects.create(
            session=chat_session,
            content=message_content,
            is_user=True, # User message
            sender_type='user'
        )

        # If user replies to a 'resolved' ticket, maybe reopen it to 'in_progress'?
        if ticket.status == 'resolved':
            ticket.status = 'in_progress'
            # Maybe unassign agent if user replies after resolution? Optional.
            # ticket.assigned_to = None
            ticket.save(update_fields=['status'])
            logger.info(f"Ticket {pk} reopened to 'in_progress' due to user reply.")
            # Add system message?
            # ChatMessage.objects.create(session=chat_session, content="Ticket reopened by user reply.", is_user=False, sender_type='system')


        logger.info(f"User {request.user.email} replied to Ticket {pk}")

        return Response(
            ChatMessageSerializer(message).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get summary counts of the current user's tickets by status"""
        user = request.user
        if not user or not user.is_authenticated:
             # Should be caught by IsAuthenticated permission class, but good safety check
              return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)


        # Use the filtered queryset logic from get_queryset
        base_queryset = self.get_queryset() # Already filtered for the user

        # Get counts by status
        status_counts = base_queryset.values('status').annotate(count=Count('id'))

        # Format as dictionary
        result = {
            'total': base_queryset.count(), # Total count for this user
            'counts': {item['status']: item['count'] for item in status_counts}
        }

        return Response(result)

# --- CreateSupportTicketView ---
class CreateSupportTicketView(APIView):
    """
    API view for authenticated users to create support tickets.
    Checks 'add_supportticket' permission.
    """
    permission_classes = [IsAuthenticated] # Base: Must be logged in

    def post(self, request):
        # Check specific permission to create tickets
        if not request.user.has_perm('quickstart.add_supportticket'):
            return Response(
                 {"detail": "You do not have permission to create support tickets."},
                 status=status.HTTP_403_FORBIDDEN
             )

        # Pass request context to serializer to automatically set the user
        serializer = CreateSupportTicketSerializer(
            data=request.data,
            context={'request': request}
        )

        if serializer.is_valid():
            try:
                 # Serializer's save method should handle setting user=request.user
                 ticket = serializer.save()
                 logger.info(f"User {request.user.email} created Support Ticket {ticket.pk}")
                 # Add to AuditLog if needed for user actions

                 # Return data using the appropriate detail or user-facing serializer
                 response_serializer = UserSupportTicketSerializer(ticket, context={'request': request}) # Or SupportTicketDetailSerializer
                 return Response(response_serializer.data, status=status.HTTP_201_CREATED)

            except Exception as e:
                 logger.error(f"Error creating support ticket for user {request.user.email}: {e}", exc_info=True)
                 return Response({"detail": "An error occurred while creating the ticket."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Return validation errors
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)