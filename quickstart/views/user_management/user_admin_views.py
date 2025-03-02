from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone
import logging

from ...models import AuditLog
from ...serializers.user_management.admin_serializers import (
    AdminUserListSerializer,
    AdminUserDetailSerializer,
    AdminUserCreateUpdateSerializer
)
from ...utils.permissions import IsAdminUser

User = get_user_model()
logger = logging.getLogger(__name__)

class UserAdminViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing users
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    filter_backends = [filters.SearchFilter]
    search_fields = ['email', 'first_name', 'last_name', 'phone_number']
    
    def get_queryset(self):
        queryset = User.objects.all()
        
        # Add role filtering
        role = self.request.query_params.get('role')
        if role:
            queryset = queryset.filter(role__name=role)
        
        # Add status filtering
        status = self.request.query_params.get('status')
        if status == 'active':
            queryset = queryset.filter(is_active=True)
        elif status == 'inactive':
            queryset = queryset.filter(is_active=False)
        elif status == 'pending':
            queryset = queryset.filter(last_login__isnull=True)
        
        # Add booking count annotation
        queryset = queryset.annotate(bookings_count=Count('bookings'))
        
        # Add ordering
        ordering = self.request.query_params.get('ordering')
        if ordering:
            if ordering == 'name':
                queryset = queryset.order_by('first_name', 'last_name')
            elif ordering == '-name':
                queryset = queryset.order_by('-first_name', '-last_name')
            else:
                queryset = queryset.order_by(ordering)
        else:
            queryset = queryset.order_by('-createdAt')
        
        return queryset
    
    def get_serializer_class(self):
        if self.action == 'list':
            return AdminUserListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return AdminUserCreateUpdateSerializer
        return AdminUserDetailSerializer
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Log the user creation
        self._log_user_action(user, 'user_create', 'User created by admin')
        
        headers = self.get_success_headers(serializer.data)
        return Response(
            AdminUserDetailSerializer(user).data, 
            status=status.HTTP_201_CREATED, 
            headers=headers
        )
    
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        
        # Store old role for logging
        old_role = instance.role.name if instance.role else None
        
        user = serializer.save()
        
        # Log role change if it occurred
        if old_role and 'role' in request.data and user.role and user.role.name != old_role:
            self._log_user_action(
                user, 
                'role_change', 
                f"Role changed from {old_role} to {user.role.name}",
                metadata={'changes': {'role': {'from': old_role, 'to': user.role.name}}}
            )
        else:
            # Log general update
            self._log_user_action(user, 'user_update', 'User information updated by admin')
        
        return Response(AdminUserDetailSerializer(user).data)
    
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        user_email = instance.email
        
        # Log the deletion
        self._log_user_action(
            instance, 
            'user_delete', 
            f"User {user_email} deleted by admin"
        )
        
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @action(detail=True, methods=['post'])
    def lock_account(self, request, pk=None):
        """Lock a user account"""
        user = self.get_object()
        
        if not user.is_active:
            return Response(
                {'detail': 'Account is already locked.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        user.is_active = False
        user.save()
        
        # Log the lock action
        self._log_user_action(
            user, 
            'account_lock', 
            f"Account locked by admin"
        )
        
        return Response({'status': 'Account locked'})
    
    @action(detail=True, methods=['post'])
    def unlock_account(self, request, pk=None):
        """Unlock a user account"""
        user = self.get_object()
        
        if user.is_active:
            return Response(
                {'detail': 'Account is already active.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        user.is_active = True
        user.save()
        
        # Log the unlock action
        self._log_user_action(
            user, 
            'account_unlock', 
            f"Account unlocked by admin"
        )
        
        return Response({'status': 'Account unlocked'})
    
    @action(detail=True, methods=['post'])
    def reset_password(self, request, pk=None):
        """Initiate password reset for a user"""
        user = self.get_object()
        
        # Generate reset token logic would go here
        # For now, we'll just log the action
        
        self._log_user_action(
            user, 
            'password_reset', 
            f"Password reset initiated by admin"
        )
        
        return Response({'status': 'Password reset initiated'})
    
    @action(detail=False, methods=['get'])
    def metrics(self, request):
        """Get metrics for user management dashboard - optimized to reduce query count"""
        from django.db.models.functions import TruncMonth
        
        # Get all basic counts in a single query using conditional aggregation
        user_counts = User.objects.aggregate(
            total_users=Count('userId'),
            active_users=Count('userId', filter=Q(is_active=True)),
            new_users=Count('userId', filter=Q(
                createdAt__gte=timezone.now() - timezone.timedelta(days=30)
            ))
        )
        
        # Get role distribution in a single query
        role_distribution = User.objects.values(
            'role__name', 'role__color'
        ).annotate(
            count=Count('userId')
        ).order_by('-count')
        
        # Get registration trend data with proper date trunc
        six_months_ago = timezone.now() - timezone.timedelta(days=180)
        registration_trend = User.objects.filter(
            createdAt__gte=six_months_ago
        ).annotate(
            month=TruncMonth('createdAt')
        ).values('month').annotate(
            count=Count('userId')
        ).order_by('month')
        
        return Response({
            'total_users': user_counts['total_users'],
            'active_users': user_counts['active_users'],
            'new_users': user_counts['new_users'],
            'role_distribution': list(role_distribution),
            'registration_trend': [
                {
                    'month': item['month'].strftime('%b'),
                    'registrations': item['count']
                }
                for item in registration_trend
            ]
        })
    
    def _log_user_action(self, target_user, action, details, metadata=None):
        """Helper method to log user actions for audit trail"""
        try:
            AuditLog.objects.create(
                user=self.request.user,
                user_email=self.request.user.email,
                action=action,
                details=details,
                target_user=target_user,
                target_model='User',
                target_id=str(target_user.userId),
                ip_address=self.request.META.get('REMOTE_ADDR'),
                user_agent=self.request.META.get('HTTP_USER_AGENT', ''),
                metadata=metadata or {}
            )
        except Exception as e:
            logger.error(f"Failed to create audit log: {str(e)}")