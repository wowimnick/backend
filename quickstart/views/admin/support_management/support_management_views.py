from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from django.db.models import Q, Count, Avg, F, ExpressionWrapper, fields, Value
from django.contrib.auth.models import Permission 
from django.contrib.contenttypes.models import ContentType 
from django.utils import timezone
from django.contrib.auth import get_user_model
import csv
from django.http import HttpResponse
from datetime import timedelta
import logging

from quickstart.serializers.public.support_chat_serializer import UserBriefSerializer

from ....models import SupportTicket, ChatMessage, ChatSession, CustomUser
from ..user_management.user_admin_views import user_can_manage 
from ....serializers import ( # Example path
    SupportTicketSerializer, ChatMessageSerializer, SupportTicketDetailSerializer
)

User = get_user_model()
logger = logging.getLogger(__name__)

# --- Custom Permission Classes ---

class CanAccessSupportAdmin(BasePermission):
    message = "You do not have permission to access support ticket administration."
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated or not request.user.is_active: return False
        return request.user.has_perm('quickstart.access_support_admin')

class CanManageTargetTicket(BasePermission):
    """ Checks if user can manage ticket based on the ticket owner's hierarchy """
    message = "You cannot manage this ticket due to hierarchy restrictions."
    def has_object_permission(self, request, view, obj):
        # obj is the SupportTicket instance
        if not request.user or not request.user.is_authenticated or not request.user.is_active: return False
        # Check hierarchy against the user who owns the ticket
        return user_can_manage(request.user, obj.user)

# --- ViewSet ---

class AdminSupportTicketViewSet(viewsets.ModelViewSet):
    """
    API endpoint for Admin/Support staff managing support tickets
    """
    permission_classes = [IsAuthenticated, CanAccessSupportAdmin] # Base permission
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['subject', 'description', 'user__email', 'user__first_name', 'user__last_name', 'assigned_to__email']
    ordering_fields = ['created_at', 'updated_at', 'status', 'priority', 'category', 'user__email', 'assigned_to__email']
    ordering = ['-created_at'] # Default ordering

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return SupportTicketDetailSerializer
        # if self.action == 'stats': # Keep if using the serializer for stats
        #     return SupportTicketStatsSerializer
        # Standard list/update uses the base serializer
        return SupportTicketSerializer

    def get_queryset(self):
        """
        Return all tickets for Admin/Support view.
        """
        # Base permission check for viewing any ticket
        if not self.request.user.has_perm('quickstart.view_supportticket'):
            logger.warning(f"User {self.request.user.email} denied access to list support tickets (missing view_supportticket perm).")
            return SupportTicket.objects.none()

        # Base query with optimizations
        queryset = SupportTicket.objects.select_related(
            'user', 'user__role', 'assigned_to', 'assigned_to__role', 'chat_session'
        ).prefetch_related(
            'chat_session__messages' # Prefetch messages if needed often
        ).all()

        # --- Filtering Logic ---
        category = self.request.query_params.get('category')
        status_param = self.request.query_params.get('status')
        priority = self.request.query_params.get('priority')
        assignee = self.request.query_params.get('assignee') # Filter by assignee ID or 'me' or 'unassigned'

        if category and category != 'all':
            queryset = queryset.filter(category=category)

        if status_param and status_param != 'all':
            queryset = queryset.filter(status=status_param)

        if priority and priority != 'all':
            queryset = queryset.filter(priority=priority)

        if assignee:
            if assignee == 'me':
                queryset = queryset.filter(assigned_to=self.request.user)
            elif assignee == 'unassigned':
                queryset = queryset.filter(assigned_to__isnull=True)
            elif assignee.isdigit():
                queryset = queryset.filter(assigned_to_id=int(assignee))

        # Search and Ordering handled by filter backends

        return queryset

    # --- Standard Actions Overridden for Permissions ---
    def list(self, request, *args, **kwargs):
        # Permission checked in get_queryset
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        # Base view permission checked in get_queryset implicitly if list is allowed
        # Optional: Add hierarchy check? Usually not needed for viewing support tickets.
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        # Allow Admins/Support to create tickets (e.g., on behalf of a user)
        if not request.user.has_perm('quickstart.add_supportticket'):
             self.permission_denied(request, message="You do not have permission to create support tickets.")

        serializer = self.get_serializer(data=request.data) # Use base serializer? Or a specific AdminCreateSerializer?
        serializer.is_valid(raise_exception=True)
        # Manually set user if provided in request, otherwise defaults to request.user? Needs Create Serializer logic.
        user_id = request.data.get('user_id')
        target_user = None
        if user_id:
            try:
                target_user = User.objects.get(pk=user_id)
            except User.DoesNotExist:
                return Response({"user_id": ["User not found."]}, status=status.HTTP_400_BAD_REQUEST)

        # Ensure create serializer handles setting the user correctly
        # For simplicity, assume serializer takes user instance or ID
        instance = serializer.save(user=target_user or request.user) # Assign target user or self
        logger.info(f"Support Ticket {instance.pk} created by Admin {request.user.email} for User {instance.user.email}")
        # Add to AuditLog
        self._log_ticket_action(instance, 'ticket_create_admin', f"Ticket created by admin for user {instance.user.email}", request)

        headers = self.get_success_headers(serializer.data)
        # Return detailed view?
        detail_serializer = SupportTicketDetailSerializer(instance, context={'request': request})
        return Response(detail_serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def partial_update(self, request, *args, **kwargs):
        # Allows admin to update fields like priority, category, maybe description?
        if not request.user.has_perm('quickstart.change_supportticket'):
            self.permission_denied(request, message="You do not have permission to modify support tickets.")

        instance = self.get_object()
        # Hierarchy check based on ticket owner
        if not user_can_manage(request.user, instance.user):
             self.permission_denied(request, message="You cannot manage this ticket due to hierarchy restrictions.")

        # Define editable fields for admin partial update
        allowed_fields = ['category', 'priority', 'subject', 'description'] # Example
        update_data = {k: v for k, v in request.data.items() if k in allowed_fields}

        if not update_data:
             return Response({"detail": "No valid fields provided for update."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(instance, data=update_data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer) # Logs internally

        # Add to AuditLog
        self._log_ticket_action(instance, 'ticket_update_admin', f"Ticket details updated by admin. Changes: {update_data}", request)

        return Response(serializer.data)

    def perform_update(self, serializer):
        instance = serializer.save()
        logger.info(f"Support Ticket {instance.pk} updated by Admin {self.request.user.email}")


    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm('quickstart.delete_supportticket'):
            self.permission_denied(request, message="You do not have permission to delete support tickets.")

        instance = self.get_object()
        # Hierarchy check
        if not user_can_manage(request.user, instance.user):
            self.permission_denied(request, message="You cannot delete this ticket due to hierarchy restrictions.")

        logger.warning(f"Support Ticket {instance.pk} deleted by Admin {request.user.email}")
        # Add to AuditLog
        self._log_ticket_action(instance, 'ticket_delete_admin', "Ticket deleted by admin", request)
        return super().destroy(request, *args, **kwargs)


    # --- Custom Actions ---

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanAccessSupportAdmin])
    def reply(self, request, pk=None):
        """Add an admin/agent reply to a support ticket (Hierarchy checked by decorator)"""
        if not request.user.has_perm('quickstart.reply_any_support_ticket'):
            self.permission_denied(request, message="You do not have permission to reply to this ticket.")

        ticket = self.get_object()
        message_content = request.data.get('message', '').strip()

        if not message_content:
            return Response({'error': 'Message content is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Get or create chat session
        # Use atomic transaction to prevent race conditions creating session
        with transaction.atomic():
             chat_session, created = ChatSession.objects.get_or_create(userId=ticket.user)
             if not ticket.chat_session:
                 ticket.chat_session = chat_session
                 ticket.save(update_fields=['chat_session'])

        # Create agent message
        message = ChatMessage.objects.create(
            session=chat_session,
            content=message_content,
            is_user=False, # Agent message
            sender_type='agent' # Or derive from user role if needed
        )

        # Update ticket status if needed (e.g., move from 'open' or 'resolved' back to 'in_progress')
        updated_fields = []
        if ticket.status == 'open':
            ticket.status = 'in_progress'
            updated_fields.append('status')
            # Assign to replying agent if unassigned
            if not ticket.assigned_to:
                 ticket.assigned_to = request.user
                 updated_fields.append('assigned_to')
        elif ticket.status == 'resolved': # Re-open if agent replies after resolution
             ticket.status = 'in_progress'
             updated_fields.append('status')

        if updated_fields:
             ticket.save(update_fields=updated_fields)

        logger.info(f"Admin {request.user.email} replied to Ticket {pk}")
        # Add to AuditLog? Maybe too verbose for replies.

        return Response(
            ChatMessageSerializer(message).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanAccessSupportAdmin])
    def assign(self, request, pk=None):
        """Assign ticket to an agent (Hierarchy checked by decorator)"""
        if not request.user.has_perm('quickstart.assign_support_ticket'):
             self.permission_denied(request, message="You do not have permission to assign tickets.")

        ticket = self.get_object()
        agent_id = request.data.get('agent_id')

        if not agent_id or not str(agent_id).isdigit():
            return Response({'error': 'Valid agent_id (integer) is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            agent = User.objects.get(userId=int(agent_id))

            # Update ticket
            old_assignee_email = ticket.assigned_to.email if ticket.assigned_to else "Unassigned"
            ticket.assigned_to = agent
            if ticket.status == 'open':
                ticket.status = 'in_progress'
            ticket.save(update_fields=['assigned_to', 'status'])

            # Add system message
            if ticket.chat_session:
                ChatMessage.objects.create(
                    session=ticket.chat_session,
                    content=f"Ticket assigned to {agent.get_full_name()}",
                    is_user=False, sender_type='system'
                )

            logger.info(f"Ticket {pk} assigned to {agent.email} by Admin {request.user.email}")
            # Add to AuditLog
            self._log_ticket_action(ticket, 'ticket_assign', f"Assigned to {agent.email} from {old_assignee_email}", request)


            # Return *updated* ticket detail
            serializer = SupportTicketDetailSerializer(ticket, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)

        except CustomUser.DoesNotExist:
            return Response({'error': 'Agent user not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanAccessSupportAdmin])
    def resolve(self, request, pk=None):
        """Resolve a ticket (Hierarchy checked by decorator)"""
        if not request.user.has_perm('quickstart.resolve_support_ticket'):
            self.permission_denied(request, message="You do not have permission to resolve tickets.")

        ticket = self.get_object()

        if ticket.status in ['resolved', 'closed']:
            return Response({'error': 'Ticket is already resolved or closed.'}, status=400)

        resolution_notes = request.data.get('resolution_notes', '').strip()
        # Make notes required? Optional.
        # if not resolution_notes:
        #    return Response({'error': 'Resolution notes are required.'}, status=400)

        # Update ticket
        ticket.status = 'resolved'
        ticket.resolution_notes = resolution_notes
        # Assign to resolver if not already assigned or assigned to someone else?
        if not ticket.assigned_to or ticket.assigned_to != request.user:
             ticket.assigned_to = request.user
             ticket.save(update_fields=['status', 'resolution_notes', 'assigned_to'])
        else:
             ticket.save(update_fields=['status', 'resolution_notes'])


        # Add system message to chat
        if ticket.chat_session:
            note_text = f": {resolution_notes}" if resolution_notes else ""
            ChatMessage.objects.create(
                session=ticket.chat_session,
                content=f"Ticket resolved by {request.user.get_full_name()}{note_text}",
                is_user=False, sender_type='system'
            )

        logger.info(f"Ticket {pk} resolved by Admin {request.user.email}")
        # Add to AuditLog
        self._log_ticket_action(ticket, 'ticket_resolve', f"Ticket resolved by admin. Notes: {resolution_notes}", request)

        serializer = SupportTicketDetailSerializer(ticket, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanAccessSupportAdmin])
    def close(self, request, pk=None):
        """Close a resolved ticket (Hierarchy checked by decorator)"""
        # Use same permission as resolve? Or a separate one? Using resolve_support_ticket for now.
        if not request.user.has_perm('quickstart.resolve_support_ticket'):
            self.permission_denied(request, message="You do not have permission to close tickets.")

        ticket = self.get_object()

        if ticket.status != 'resolved':
            return Response({'error': 'Only resolved tickets can be closed.'}, status=400)

        # Update ticket
        ticket.status = 'closed'
        ticket.save(update_fields=['status'])

        # Add system message
        if ticket.chat_session:
            ChatMessage.objects.create(
                session=ticket.chat_session,
                content="Ticket has been closed.",
                is_user=False, sender_type='system'
            )

        logger.info(f"Ticket {pk} closed by Admin {request.user.email}")
        # Add to AuditLog
        self._log_ticket_action(ticket, 'ticket_close', "Ticket closed by admin", request)


        serializer = SupportTicketDetailSerializer(ticket, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)
    
    @action(detail=False, methods=['get'], url_path='assignable-agents')
    def list_assignable_agents(self, request):
        """
        Lists users who have the permission to resolve/handle support tickets.
        Used for populating the assignment modal dropdown.
        """
        # Permission check: Anyone who can access support admin should be able to see the agent list
        if not request.user.has_perm('quickstart.access_support_admin'):
            self.permission_denied(request, message="You cannot view assignable agents.")

        try:
            # --- Find users with the 'resolve_support_ticket' permission ---
            # This permission implies they can work on tickets. Adjust if you use a different permission.
            content_type = ContentType.objects.get_for_model(SupportTicket)
            resolve_permission = Permission.objects.get(
                content_type=content_type,
                codename='resolve_support_ticket' # Make sure this matches the codename in models.py
            )

            # Get users who have this permission directly OR through one of their groups/roles
            agent_users = User.objects.filter(
                Q(is_active=True) & # Only active users
                (
                   Q(role__permissions=resolve_permission) # Check permissions via assigned Role model
                )
            ).select_related('role').distinct().order_by('first_name', 'last_name')

            # Serialize the results (using a lightweight serializer is best)
            # You might want to create a dedicated AdminAgentSerializer for this
            # Using UserBriefSerializer for now
            serializer = UserBriefSerializer(agent_users, many=True)
            return Response(serializer.data)

        except Permission.DoesNotExist:
             logger.error("Permission 'resolve_support_ticket' for SupportTicket model not found. Run migrations or check models.py.")
             return Response({"error": "Required permission 'resolve_support_ticket' not found."}, status=500)
        except Exception as e:
            logger.error(f"Error fetching assignable agents: {e}", exc_info=True)
            return Response({"error": "Could not retrieve assignable agents."}, status=500)

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get ticket statistics for dashboard"""
        if not request.user.has_perm('quickstart.view_support_ticket_stats'):
            self.permission_denied(request, message="You do not have permission to view ticket statistics.")

        try:
            status_counts = SupportTicket.objects.values('status').annotate(count=Count('ticket_id')) # Use ticket_id
            status_dict = {item['status']: item['count'] for item in status_counts}

            today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
            tickets_today = SupportTicket.objects.filter(created_at__gte=today_start).count() # count() is fine here

            thirty_days_ago = timezone.now() - timedelta(days=30)
            resolved_tickets = SupportTicket.objects.filter(
                status__in=['resolved', 'closed'],
                updated_at__gte=F('created_at'),
                created_at__gte=thirty_days_ago
            ).annotate(
                resolution_time=ExpressionWrapper(
                    F('updated_at') - F('created_at'), output_field=fields.DurationField()
                )
            )
            avg_resolution_data = resolved_tickets.aggregate(avg=Avg('resolution_time'))
            avg_resolution_duration = avg_resolution_data.get('avg')

            avg_resolution_formatted = "N/A"
            if avg_resolution_duration:
                 total_seconds = avg_resolution_duration.total_seconds()
                 days = total_seconds // 86400
                 hours = (total_seconds % 86400) // 3600
                 if days > 0:
                     avg_resolution_formatted = f"{days:.1f} days"
                 elif hours > 0:
                      avg_resolution_formatted = f"{hours:.1f} hours"
                 else:
                      avg_resolution_formatted = f"{total_seconds / 60:.1f} mins"

            categories = list(SupportTicket.objects.values(
                name=F('category')
                ).annotate(value=Count('ticket_id')).order_by('-value')) # Use ticket_id

            agent_tickets = list(SupportTicket.objects.filter(assigned_to__isnull=False)
                                .values(label=F('assigned_to__email'))
                                .annotate(value=Count('ticket_id')).order_by('-value')[:5]) # Use ticket_id

            total_tickets = SupportTicket.objects.count() # count() without args is efficient

            data = {
                'total_tickets': total_tickets,
                'open_tickets': status_dict.get('open', 0),
                'in_progress_tickets': status_dict.get('in_progress', 0),
                'resolved_tickets': status_dict.get('resolved', 0),
                'closed_tickets': status_dict.get('closed', 0),
                'tickets_today': tickets_today,
                'avg_resolution_time_display': avg_resolution_formatted,
                'avg_resolution_time_seconds': avg_resolution_duration.total_seconds() if avg_resolution_duration else None,
                'category_distribution': categories,
                'agent_ticket_load': agent_tickets,
            }
            logger.info(f"Successfully generated support ticket stats: {data}") # Log success
            return Response(data)

        except Exception as e:
            logger.error(f"Error generating support ticket stats: {e}", exc_info=True)
            return Response({"error": "Could not generate statistics."}, status=500) # Keep status 500


    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export tickets to CSV"""
        if not request.user.has_perm('quickstart.export_support_ticket_data'):
            self.permission_denied(request, message="You do not have permission to export tickets.")

        try:
            queryset = self.filter_queryset(self.get_queryset()) # Apply filters

            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="support_tickets_export.csv"'
            writer = csv.writer(response)

            writer.writerow([
                'Ticket ID', 'Subject', 'Category', 'Status', 'Priority',
                'User Name', 'User Email', 'Created Date', 'Last Updated', 'Assigned To',
                'Resolution Notes'
            ])

            # Use iterator for memory efficiency on large exports
            for ticket in queryset.iterator():
                writer.writerow([
                    ticket.ticket_id,
                    ticket.subject,
                    ticket.get_category_display(),
                    ticket.get_status_display(),
                    ticket.get_priority_display(),
                    ticket.user.get_full_name() if ticket.user else 'N/A',
                    ticket.user.email if ticket.user else 'N/A',
                    ticket.created_at.strftime('%Y-%m-%d %H:%M'),
                    ticket.updated_at.strftime('%Y-%m-%d %H:%M'),
                    ticket.assigned_to.get_full_name() if ticket.assigned_to else "Unassigned",
                    ticket.resolution_notes
                ])

            return response
        except Exception as e:
             logger.error(f"Error exporting support tickets: {e}", exc_info=True)
             return HttpResponse(f"Error exporting data: {str(e)}", status=500, content_type="text/plain")

    def _log_ticket_action(self, ticket, action_code, details, request):
        """ Helper to log ticket related actions """
        from ....models import AuditLog 
        try:
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action=action_code, # Use specific codes like 'ticket_assign', 'ticket_resolve'
                details=details,
                target_user=ticket.user, # Target is the user who owns the ticket
                target_model='SupportTicket',
                target_id=str(ticket.ticket_id),
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                metadata={ # Add relevant ticket metadata
                    'ticket_subject': ticket.subject[:100], # Truncate subject
                    'ticket_status': ticket.status,
                    'assigned_to': ticket.assigned_to.email if ticket.assigned_to else None,
                }
            )
        except Exception as e:
             logger.error(f"Failed to create audit log for ticket action {action_code}: {str(e)}", exc_info=True)