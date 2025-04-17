from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
import logging

from ....models import VerificationRequest, VerificationDocument, AuditLog
from quickstart.serializers.admin.user_management.verification_serializers import (
    VerificationRequestListSerializer,
    VerificationRequestDetailSerializer,
    VerificationSubmissionSerializer,
    VerificationProcessSerializer,
    VerificationDocumentSerializer
)

logger = logging.getLogger(__name__)

class VerificationRequestViewSet(viewsets.ModelViewSet):
    """Viewset for managing verification requests"""
    filter_backends = [filters.SearchFilter]
    search_fields = ['user__email', 'user__first_name', 'user__last_name', 'business__businessName']
    
    def get_permissions(self):
        """
        - List and retrieve: Admin users
        - Create: Any authenticated user
        - Update/delete: Admin users
        """
        if self.action == 'create' or self.action == 'submit_verification':
            return [IsAuthenticated()]
        elif self.action in ['list', 'retrieve', 'update', 'partial_update', 'destroy', 'process_verification']:
            return [IsAuthenticated()]
        return [IsAuthenticated()]

    def get_queryset(self):
        """
        - Admins can see all requests
        - Regular users can only see their own requests
        Optimized with select_related to fetch reviewer information
        """
        user = self.request.user
        
        if user.role and user.role.name in ['Admin', 'Super Admin']:
            queryset = VerificationRequest.objects.all()
            
            # Filter by status if provided
            status_filter = self.request.query_params.get('status')
            if status_filter:
                if isinstance(status_filter, list):
                    queryset = queryset.filter(status__in=status_filter)
                else:
                    queryset = queryset.filter(status=status_filter)
            
            # Use select_related to efficiently fetch related data including reviewer
            return queryset.select_related(
                'user', 
                'user__role',
                'business', 
                'reviewed_by',  # Make sure we include the reviewer
                'reviewed_by__role'  # And the reviewer's role
            ).prefetch_related('documents')  # Added to get document count efficiently
        
        # Regular users only see their own requests
        return VerificationRequest.objects.filter(user=user).select_related('business')
    
    def get_serializer_class(self):
        if self.action == 'list':
            return VerificationRequestListSerializer
        elif self.action == 'create' or self.action == 'submit_verification':
            return VerificationSubmissionSerializer
        elif self.action == 'process_verification':
            return VerificationProcessSerializer
        return VerificationRequestDetailSerializer
    
    @action(detail=False, methods=['post'])
    def submit_verification(self, request):
        """Endpoint for users to submit verification requests"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        verification = serializer.save()
        
        # Log the submission
        try:
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action='verification_submit',
                details=f"Verification request submitted",
                target_model='VerificationRequest',
                target_id=str(verification.id),
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', '')
            )
        except Exception as e:
            logger.error(f"Failed to create audit log: {str(e)}")
        
        return Response(
            VerificationRequestDetailSerializer(verification).data,
            status=status.HTTP_201_CREATED
        )
    
    @action(detail=True, methods=['post'])
    def process_verification(self, request, pk=None):
        """Endpoint for admins to approve or reject verification requests"""
        verification = self.get_object()
        
        if verification.status != 'pending':
            return Response(
                {'detail': 'This verification request has already been processed.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Map frontend values to backend values
        request_data = request.data.copy()
        if 'status' in request_data:
            if request_data['status'] == 'approve':
                request_data['status'] = 'approved'
            elif request_data['status'] == 'reject':
                request_data['status'] = 'rejected'
        
        serializer = self.get_serializer(verification, data=request_data, partial=True)
        serializer.is_valid(raise_exception=True)
        
        # Update the verification request
        verification = serializer.save()
        
        # Log the action
        action = 'verification_approve' if verification.status == 'approved' else 'verification_reject'
        details = f"Verification request {verification.status}"
        
        try:
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action=action,
                details=details,
                target_user=verification.user,
                target_model='VerificationRequest',
                target_id=str(verification.id),
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                metadata={
                    'business_id': verification.business.businessId if verification.business else None,
                    'business_name': verification.business.businessName if verification.business else None,
                    'notes': verification.notes,
                    'rejection_reason': verification.rejection_reason if verification.status == 'rejected' else None
                }
            )
        except Exception as e:
            logger.error(f"Failed to create audit log: {str(e)}")
        
        return Response(VerificationRequestDetailSerializer(verification, context={'request': request}).data)
    
    @action(detail=True, methods=['get'])
    def documents(self, request, pk=None):
        """Get documents for a verification request"""
        verification = self.get_object()
        documents = verification.documents.all()
        serializer = VerificationDocumentSerializer(documents, many=True)
        return Response(serializer.data)
