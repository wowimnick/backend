from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
import logging

from quickstart.utils.permissions import CanProcessVerificationRequests, CanViewAllVerificationRequests

# --- MODIFICATION: Import new email function and User model ---
from ....models import VerificationRequest, VerificationDocument, AuditLog, CustomUser
from django.contrib.auth import get_user_model # Get User model helper
from django.contrib.auth.models import Permission # For finding admins
from django.contrib.contenttypes.models import ContentType # For finding admins
# --- END MODIFICATION ---

from quickstart.serializers.admin.user_management.verification_serializers import (
    VerificationRequestListSerializer,
    VerificationRequestDetailSerializer,
    VerificationSubmissionSerializer,
    VerificationProcessSerializer,
    VerificationDocumentSerializer
)

# --- MODIFICATION: Import new email function ---
from ....utils.email_utils import (
        send_business_verification_approved_email,
        send_business_verification_rejected_email,
        send_admin_new_verification_request_email # Import the new function
    )
# --- END MODIFICATION ---

logger = logging.getLogger(__name__)
User = get_user_model() # Get the CustomUser model

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
        verification = serializer.save() # Serializer should handle user assignment

        # Log the submission
        try:
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action='verification_submit',
                details=f"Verification request submitted for business ID {verification.business.businessId if verification.business else 'N/A'}", # Added business ID
                target_model='VerificationRequest',
                target_id=str(verification.id),
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', '')
            )
        except Exception as e:
            logger.error(f"Failed to create audit log for verification submission: {str(e)}")

        try:
            # Define how to find admins - e.g., by permission
            content_type = ContentType.objects.get_for_model(VerificationRequest)
            admin_perm = Permission.objects.get(content_type=content_type, codename='process_verificationrequest')
            admin_users = User.objects.filter(
                 Q(is_superuser=True) | Q(groups__permissions=admin_perm) | Q(user_permissions=admin_perm)
            ).filter(is_active=True, email__isnull=False).exclude(email='').distinct()

            admin_emails = list(admin_users.values_list('email', flat=True))

            if admin_emails:
                send_admin_new_verification_request_email(admin_emails, verification)
                logger.info(f"Admin notification queued for verification request {verification.id}")
            else:
                 logger.warning(f"No active admin users found with 'process_verificationrequest' permission to notify about verification {verification.id}")

        except Permission.DoesNotExist:
            logger.error("Permission 'process_verificationrequest' not found. Cannot notify admins.")
        except Exception as e:
             logger.error(f"Failed to send admin notification email for verification {verification.id}: {e}", exc_info=True)

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

        # Use the VerificationProcessSerializer to validate the incoming data
        # It expects 'status' to be 'approve' or 'reject' and validates 'notes' if rejecting
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True) # Will raise 400 if invalid

        # --- If validation passes, proceed to update the model ---
        validated_data = serializer.validated_data
        action_input = validated_data.get('status') # This is 'approve' or 'reject'
        notes = validated_data.get('notes', '')
        rejection_reason_from_notes = notes if action_input == 'reject' else ''

        # Map the validated action to the model's status
        target_db_status = None
        if action_input == 'approve':
            target_db_status = 'approved'
        elif action_input == 'reject':
            target_db_status = 'rejected'

        if target_db_status is None:
            # Should not happen if serializer validation works, but safeguard
            logger.error(f"Internal logic error: Validated action '{action_input}' did not map to a DB status for verification {pk}.")
            return Response({'error': 'Internal processing error.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # --- Update the VerificationRequest model instance directly ---
        verification.status = target_db_status
        verification.notes = notes # Save the notes regardless of approve/reject
        verification.rejection_reason = rejection_reason_from_notes # Set rejection reason only if rejecting
        verification.reviewed_by = request.user
        verification.reviewed_at = timezone.now()
        verification.save() # Save the updated fields

        # --- Post-save actions (Logging and Email) ---
        action_code = None
        log_details = f"Verification request {verification.status}"
        if verification.status == 'approved':
            action_code = 'verification_approve'
            try:
                if verification.user and verification.business:
                    send_business_verification_approved_email(
                        user=verification.user, business=verification.business
                    )
                    logger.info(f"Verification approved email prepared/queued for business {verification.business.businessId}, user {verification.user.email}")
                else:
                    logger.error(f"Cannot send approval email for verification {verification.id}: Missing user or business link.")
            except Exception as email_error:
                logger.error(f"Failed to send verification approved email for {verification.id}: {email_error}", exc_info=True)

        elif verification.status == 'rejected':
            action_code = 'verification_reject'
            log_details += f". Reason: {verification.rejection_reason or 'None provided'}"
            try:
                if verification.user and verification.business:
                     send_business_verification_rejected_email(
                         user=verification.user, business=verification.business,
                         verification_request=verification
                     )
                     logger.info(f"Verification rejected email prepared/queued for business {verification.business.businessId}, user {verification.user.email}")
                else:
                     logger.error(f"Cannot send rejection email for verification {verification.id}: Missing user or business link.")
            except Exception as email_error:
                logger.error(f"Failed to send verification rejected email for {verification.id}: {email_error}", exc_info=True)

        if action_code:
            try:
                AuditLog.objects.create(
                    user=request.user, user_email=request.user.email, action=action_code,
                    details=log_details, target_user=verification.user,
                    target_model='VerificationRequest', target_id=str(verification.id),
                    ip_address=request.META.get('REMOTE_ADDR'), user_agent=request.META.get('HTTP_USER_AGENT', ''),
                    metadata={
                        'business_id': verification.business.businessId if verification.business else None,
                        'business_name': verification.business.businessName if verification.business else None,
                        'notes': verification.notes,
                        'rejection_reason': verification.rejection_reason if verification.status == 'rejected' else None
                    }
                )
            except Exception as e:
                logger.error(f"Failed to create audit log for verification processing: {str(e)}")

        # Return updated details using the detail serializer
        return Response(VerificationRequestDetailSerializer(verification, context={'request': request}).data)

    @action(detail=True, methods=['get'])
    def documents(self, request, pk=None):
        """Get documents for a verification request"""
        verification = self.get_object()
        documents = verification.documents.all()
        serializer = VerificationDocumentSerializer(documents, many=True)
        return Response(serializer.data)