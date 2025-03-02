from rest_framework import viewsets, permissions
from rest_framework.response import Response
from rest_framework import status

from quickstart.utils.permissions import check_user_role
from quickstart.models import SupportTicket
from ..serializers import SupportTicketSerializer

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
    serializer_class = SupportTicketSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrSupport]
    
    def get_queryset(self):
        user = self.request.user
        # Support staff can see all tickets
        if check_user_role(self.request.user, ['Admin', 'Business Owner', 'Manager']): 
            return SupportTicket.objects.all()
        # Regular users can only see their own tickets
        return SupportTicket.objects.filter(user=user)
    
    def perform_update(self, serializer):
        # Record who made changes
        if 'status' in serializer.validated_data and serializer.validated_data['status'] in ['in_progress', 'resolved']:
            serializer.save(assigned_to=self.request.user)
        else:
            serializer.save()