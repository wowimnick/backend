from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework import viewsets, permissions
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count, Avg, F, ExpressionWrapper, fields
from django.utils import timezone
from rest_framework.views import APIView
import csv
from django.http import HttpResponse

from quickstart.models import SupportTicket, ChatMessage, ChatSession, CustomUser
from quickstart.utils.permissions import check_user_role
from ..serializers import SupportTicketSerializer, ChatMessageSerializer, SupportTicketDetailSerializer, SupportTicketStatsSerializer, CreateSupportTicketSerializer, UserSupportTicketSerializer

class IsOwnerOrSupport(permissions.BasePermission):
    """
    Custom permission to only allow owners of a ticket or support staff to view/edit it
    """
    def has_object_permission(self, request, view, obj):
        # Support roles can access any ticket
        if request.user.role and request.user.role.name in ['admin', 'support']:
            return True
        # Otherwise, users can only access their own tickets
        return obj.user == request.user

class SupportTicketViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing support tickets
    """
    serializer_class = SupportTicketSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['subject', 'description', 'user__email', 'user__first_name', 'user__last_name']
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return SupportTicketDetailSerializer
        if self.action == 'stats':
            return SupportTicketStatsSerializer
        return SupportTicketSerializer
    
    def get_queryset(self):
        """
        Return tickets based on user role:
        - Admin/support: all tickets
        - Regular users: only their own tickets
        """
        user = self.request.user
        
        # Base query with annotations
        queryset = SupportTicket.objects.select_related(
            'user', 'assigned_to', 'chat_session'
        ).prefetch_related(
            'chat_session__messages'
        )
        
        # Apply filters based on query parameters
        category = self.request.query_params.get('category')
        status_param = self.request.query_params.get('status')
        priority = self.request.query_params.get('priority')
        view_mode = self.request.query_params.get('view')
        
        if category and category != 'all':
            queryset = queryset.filter(category=category)
            
        if status_param and status_param != 'all':
            queryset = queryset.filter(status=status_param)
            
        if priority and priority != 'all':
            queryset = queryset.filter(priority=priority)
            
        if view_mode == 'assigned_to_me':
            queryset = queryset.filter(assigned_to=user)
        elif view_mode == 'created_today':
            today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
            queryset = queryset.filter(created_at__gte=today)
            
        return queryset.order_by('-created_at')
    
    @action(detail=True, methods=['post'])
    def reply(self, request, pk=None):
        """Add a reply to a support ticket"""
        ticket = self.get_object()
        
        # Validate request data
        if 'message' not in request.data or not request.data['message'].strip():
            return Response(
                {'error': 'Message content is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Get or create chat session if needed
        chat_session = ticket.chat_session
        if not chat_session:
            chat_session = ChatSession.objects.create(userId=ticket.user)
            ticket.chat_session = chat_session
            ticket.save(update_fields=['chat_session'])
        
        # Create agent message
        message = ChatMessage.objects.create(
            session=chat_session,
            content=request.data['message'],
            is_user=False,
            sender_type='agent'
        )
        
        # If ticket is open, move to in_progress
        if ticket.status == 'open':
            ticket.status = 'in_progress'
            ticket.assigned_to = request.user
            ticket.save(update_fields=['status', 'assigned_to'])
        
        return Response(
            ChatMessageSerializer(message).data,
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        """Assign ticket to an agent"""
        ticket = self.get_object()
        
        if 'agent_id' not in request.data:
            return Response(
                {'error': 'agent_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        try:
            agent = CustomUser.objects.get(userId=request.data['agent_id'])
            
            # Check if agent has support role
            if not check_user_role(agent, ['Admin', 'Manager', 'Business Owner']):
                return Response(
                    {'error': 'Selected user does not have permission to handle tickets'},
                    status=status.HTTP_400_BAD_REQUEST
                )
                
            # Update ticket
            ticket.assigned_to = agent
            if ticket.status == 'open':
                ticket.status = 'in_progress'
            ticket.save(update_fields=['assigned_to', 'status'])
            
            # Add system message in chat about assignment
            if ticket.chat_session:
                ChatMessage.objects.create(
                    session=ticket.chat_session,
                    content=f"Ticket assigned to {agent.first_name} {agent.last_name}",
                    is_user=False,
                    sender_type='system'
                )
            
            return Response(
                SupportTicketSerializer(ticket).data,
                status=status.HTTP_200_OK
            )
            
        except CustomUser.DoesNotExist:
            return Response(
                {'error': 'Agent not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=True, methods=['post'])
    def resolve(self, request, pk=None):
        """Resolve a ticket"""
        ticket = self.get_object()
        
        # Can't resolve already resolved tickets
        if ticket.status in ['resolved', 'closed']:
            return Response(
                {'error': 'Ticket is already resolved or closed'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Get resolution notes
        resolution_notes = request.data.get('resolution_notes', '')
        
        # Update ticket
        ticket.status = 'resolved'
        ticket.resolution_notes = resolution_notes
        ticket.assigned_to = request.user  # Assign to current user if not already assigned
        ticket.save(update_fields=['status', 'resolution_notes', 'assigned_to'])
        
        # Add resolution note to chat
        if ticket.chat_session and resolution_notes:
            ChatMessage.objects.create(
                session=ticket.chat_session,
                content=f"Ticket resolved: {resolution_notes}",
                is_user=False,
                sender_type='system'
            )
        
        return Response(
            SupportTicketSerializer(ticket).data,
            status=status.HTTP_200_OK
        )
    
    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """Close a resolved ticket"""
        ticket = self.get_object()
        
        # Can only close resolved tickets
        if ticket.status != 'resolved':
            return Response(
                {'error': 'Only resolved tickets can be closed'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Update ticket
        ticket.status = 'closed'
        ticket.save(update_fields=['status'])
        
        # Add system message to chat
        if ticket.chat_session:
            ChatMessage.objects.create(
                session=ticket.chat_session,
                content="Ticket has been closed",
                is_user=False,
                sender_type='system'
            )
        
        return Response(
            SupportTicketSerializer(ticket).data,
            status=status.HTTP_200_OK
        )
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get ticket statistics for dashboard"""
        # Ensure user has appropriate permissions
        if not check_user_role(request.user, ['Admin', 'Super Admin']):
            return Response(
                {'error': 'You do not have permission to view ticket statistics'},
                status=status.HTTP_403_FORBIDDEN
            )
            
        # Get current counts by status
        status_counts = SupportTicket.objects.values('status').annotate(count=Count('status'))
        status_dict = {item['status']: item['count'] for item in status_counts}
        
        # Get count of tickets created today
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tickets_today = SupportTicket.objects.filter(created_at__gte=today).count()
        
        # Calculate average resolution time for tickets created in last 30 days
        thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
        resolved_tickets = SupportTicket.objects.filter(
            status__in=['resolved', 'closed'],
            created_at__gte=thirty_days_ago
        )
        
        # Calculate time difference between created_at and updated_at for resolved tickets
        resolution_time_expr = ExpressionWrapper(
            F('updated_at') - F('created_at'),
            output_field=fields.DurationField()
        )
        avg_resolution = resolved_tickets.annotate(
            resolution_time=resolution_time_expr
        ).aggregate(avg=Avg('resolution_time'))
        
        # Format average resolution time
        avg_resolution_days = None
        if avg_resolution['avg']:
            avg_resolution_days = round(avg_resolution['avg'].total_seconds() / 86400, 1)
            
        # Get category distribution
        categories = SupportTicket.objects.values('category').annotate(
            count=Count('category')
        ).order_by('-count')
        
        # Get weekly metrics for response times
        # (In a real implementation, would calculate based on actual response times)
        # For now, using mock data for demonstration
        
        response_times = {
            'last_week': '4.5 hours',
            'this_week': '3.2 hours',
            'improvement': '28.9%'
        }
        
        data = {
            'open_tickets': status_dict.get('open', 0),
            'in_progress_tickets': status_dict.get('in_progress', 0),
            'tickets_today': tickets_today,
            'avg_resolution_time': f"{avg_resolution_days} days" if avg_resolution_days else "N/A",
            'category_distribution': [
                {'name': item['category'].capitalize(), 'value': item['count']}
                for item in categories
            ],
            'response_times': response_times
        }
        
        return Response(data, status=status.HTTP_200_OK)
    
    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export tickets to CSV"""
        # Ensure user has appropriate permissions
        if not check_user_role(request.user, ['Admin', 'Manager', 'Business Owner']):
            return Response(
                {'error': 'You do not have permission to export tickets'},
                status=status.HTTP_403_FORBIDDEN
            )
            
        # Get filtered queryset
        queryset = self.filter_queryset(self.get_queryset())
        
        # Create the HttpResponse with CSV header
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="support_tickets.csv"'
        
        # Create CSV writer
        writer = csv.writer(response)
        writer.writerow([
            'Ticket ID', 'Subject', 'Category', 'Status', 'Priority',
            'User', 'Email', 'Created Date', 'Assigned To', 'Resolution Notes'
        ])
        
        # Add data rows
        for ticket in queryset:
            writer.writerow([
                ticket.ticket_id,
                ticket.subject,
                ticket.get_category_display(),
                ticket.get_status_display(),
                ticket.get_priority_display(),
                f"{ticket.user.first_name} {ticket.user.last_name}",
                ticket.user.email,
                ticket.created_at.strftime('%Y-%m-%d %H:%M'),
                f"{ticket.assigned_to.first_name} {ticket.assigned_to.last_name}" if ticket.assigned_to else "Unassigned",
                ticket.resolution_notes
            ])
            
        return response
    
class UserSupportTicketViewSet(viewsets.ModelViewSet):
    """
    API endpoint for users to view their own support tickets
    """
    serializer_class = SupportTicketSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['subject', 'description']
    
    def get_serializer_class(self):
        if self.action == 'create':
            return CreateSupportTicketSerializer
        if self.action == 'retrieve':
            return SupportTicketDetailSerializer
        return SupportTicketSerializer
    
    def get_queryset(self):
        """
        Return only tickets belonging to the current user
        """
        user = self.request.user
        
        # Base query with annotations
        queryset = SupportTicket.objects.select_related(
            'user', 'assigned_to', 'chat_session'
        ).prefetch_related(
            'chat_session__messages'
        ).filter(user=user)
        
        # Filter based on query parameters
        status_param = self.request.query_params.get('status')
        category = self.request.query_params.get('category')
        
        if status_param and status_param != 'all':
            queryset = queryset.filter(status=status_param)
            
        if category and category != 'all':
            queryset = queryset.filter(category=category)
            
        return queryset.order_by('-created_at')
    
    @action(detail=True, methods=['post'])
    def reply(self, request, pk=None):
        """Add a user reply to a support ticket"""
        ticket = self.get_object()
        
        # Validate request data
        if 'message' not in request.data or not request.data['message'].strip():
            return Response(
                {'error': 'Message content is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Get or create chat session if needed
        chat_session = ticket.chat_session
        if not chat_session:
            chat_session = ChatSession.objects.create(userId=request.user)
            ticket.chat_session = chat_session
            ticket.save(update_fields=['chat_session'])
        
        # Create user message
        message = ChatMessage.objects.create(
            session=chat_session,
            content=request.data['message'],
            is_user=True,
            sender_type='user' 
        )
        
        return Response(
            ChatMessageSerializer(message).data,
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get summary counts of user tickets by status"""
        user = request.user
        
        # Get counts by status
        status_counts = SupportTicket.objects.filter(user=user).values('status').annotate(count=Count('status'))
        
        # Format as dictionary
        result = {
            'total': SupportTicket.objects.filter(user=user).count(),
            'counts': {item['status']: item['count'] for item in status_counts}
        }
        
        return Response(result)
    

class CreateSupportTicketView(APIView):
    """
    API view for users to create support tickets
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        # Create serializer with context containing request
        serializer = CreateSupportTicketSerializer(
            data=request.data,
            context={'request': request}
        )
        
        if serializer.is_valid():
            # Create the ticket
            ticket = serializer.save()
            
            # Return the created ticket
            return Response(
                UserSupportTicketSerializer(ticket).data,
                status=status.HTTP_201_CREATED
            )
        
        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )