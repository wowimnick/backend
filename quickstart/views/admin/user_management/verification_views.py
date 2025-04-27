from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
import logging

from quickstart.utils.permissions import CanProcessVerificationRequests, CanViewAllVerificationRequests

from ....models import VerificationRequest, VerificationDocument, AuditLog
from quickstart.serializers.admin.user_management.verification_serializers import (
    VerificationRequestListSerializer,
    VerificationRequestDetailSerializer,
    VerificationSubmissionSerializer,
    VerificationProcessSerializer,
    VerificationDocumentSerializer
)

from ....utils.email_utils import (
        send_business_verification_approved_email,
        send_business_verification_rejected_email
    )

logger = logging.getLogger(__name__)

class VerificationRequestViewSet(viewsets.ModelViewSet):
    """Viewset for managing verification requests"""
    filter_backends = [filters.SearchFilter]
    search_fields = ['user__email', 'user__first_name', 'user__last_name', 'business__businessName']
    
    def get_permissions(self):
        if self.action in ['create', 'submit_verification']:
            return [IsAuthenticated()]
        elif self.action in ['list', 'retrieve']: # Viewing all/any
            # Check if user is trying to view their own or all
            # This needs refinement based on URL pattern (detail vs list)
            # Simple approach: If list, require view_all. If retrieve, check ownership or view_all.
            # For now, let's assume list/retrieve require admin view perm
             return [IsAuthenticated(), CanViewAllVerificationRequests()]
        elif self.action in ['update', 'partial_update', 'destroy', 'process_verification']: # Admin actions
            return [IsAuthenticated(), CanProcessVerificationRequests()]
        # Fallback for other actions if any
        return [IsAuthenticated()]

    def get_queryset(self):
            """
            Returns the queryset for the viewset.
            Permissions are checked before this method is called for list/retrieve actions.
            If execution reaches here for list/retrieve, the user must have the necessary permissions.
            """
            user = self.request.user # Keep user for context if needed elsewhere

            # Always start with all VerificationRequests for this admin viewset's scope.
            queryset = VerificationRequest.objects.all()
            if user.has_perm('quickstart.view_all_verificationrequests'):
                status_filter = self.request.query_params.get('status')
                if status_filter:
                    status_list = status_filter.split(',') if isinstance(status_filter, str) else status_filter
                    valid_statuses = [s.strip() for s in status_list if s.strip()]
                    if valid_statuses:
                        queryset = queryset.filter(status__in=valid_statuses)

            # Apply select_related/prefetch_related for optimization
            queryset = queryset.select_related(
                'user',
                'user__role',
                'business',
                'reviewed_by',
                'reviewed_by__role'
            ).prefetch_related('documents')

            if not user.has_perm('quickstart.view_all_verificationrequests'):
                logger.warning(f"User {user.email} without 'view_all_verificationrequests' reached get_queryset in Admin VerificationViewSet. Action: {self.action}")
                return VerificationRequest.objects.none() # Return empty

            return queryset
    
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
        verification = self.get_object() # Applies permissions/queryset

        if verification.status != 'pending':
            return Response(
                {'detail': 'This verification request has already been processed.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Map frontend values to backend values
        request_data = request.data.copy()
        new_status_input = request_data.get('status')
        target_status = None
        if new_status_input == 'approve':
            target_status = 'approved'
            request_data['status'] = target_status
        elif new_status_input == 'reject':
            target_status = 'rejected'
            request_data['status'] = target_status
        else:
             # Allow updating notes without changing status if needed,
             # but require 'approve' or 'reject' to trigger status change and email
             if 'status' in request_data: # Only error if status was provided but invalid
                 return Response({'status': ['Invalid status action provided. Use "approve" or "reject".']}, status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(verification, data=request_data, partial=True)
        serializer.is_valid(raise_exception=True)

        # Update the verification request
        verification = serializer.save( # Save assigns reviewed_by, reviewed_at handled by serializer/model
            reviewed_by=request.user,
            reviewed_at=timezone.now()
        )

        action_code = None
        log_details = f"Verification request {verification.status}"
        if verification.status == 'approved':
            action_code = 'verification_approve'
            # *** ADDED: Trigger Approved Email ***
            try:
                # Ensure user and business exist before sending
                if verification.user and verification.business:
                    send_business_verification_approved_email(
                        user=verification.user,
                        business=verification.business
                    )
                    logger.info(f"Verification approved email prepared/queued for business {verification.business.businessId}, user {verification.user.email}")
                else:
                    logger.error(f"Cannot send approval email for verification {verification.id}: Missing user or business link.")
            except Exception as email_error:
                logger.error(f"Failed to send verification approved email for {verification.id}: {email_error}", exc_info=True)

        elif verification.status == 'rejected':
            action_code = 'verification_reject'
            log_details += f". Reason: {verification.rejection_reason or 'None provided'}"
            # *** ADDED: Trigger Rejected Email ***
            try:
                if verification.user and verification.business:
                     send_business_verification_rejected_email(
                         user=verification.user,
                         business=verification.business,
                         verification_request=verification # Pass the request object itself
                     )
                     logger.info(f"Verification rejected email prepared/queued for business {verification.business.businessId}, user {verification.user.email}")
                else:
                     logger.error(f"Cannot send rejection email for verification {verification.id}: Missing user or business link.")
            except Exception as email_error:
                logger.error(f"Failed to send verification rejected email for {verification.id}: {email_error}", exc_info=True)

        # Log the action if status changed
        if action_code:
            try:
                AuditLog.objects.create(
                    user=request.user,
                    user_email=request.user.email,
                    action=action_code,
                    details=log_details,
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
                logger.error(f"Failed to create audit log for verification processing: {str(e)}")

        # Return updated details
        return Response(VerificationRequestDetailSerializer(verification, context={'request': request}).data)
    
    @action(detail=True, methods=['get'])
    def documents(self, request, pk=None):
        """Get documents for a verification request"""
        verification = self.get_object()
        documents = verification.documents.all()
        serializer = VerificationDocumentSerializer(documents, many=True)
        return Response(serializer.data)
