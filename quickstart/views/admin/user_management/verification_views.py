# quickstart/views/admin/user_management/verification_views.py

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
import logging
from django.db.models import Q

from quickstart.models import (
    VerificationRequest,
    VerificationDocument,
    AuditLog,
    CustomUser,
    Role,
    BusinessInfo,
)  # <<< ADDED Role, BusinessInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType

from quickstart.serializers.admin.user_management.verification_serializers import (
    VerificationRequestListSerializer,
    VerificationRequestDetailSerializer,
    VerificationSubmissionSerializer,
    VerificationProcessSerializer,
    VerificationDocumentSerializer,
)

from quickstart.utils.email_utils import (
    send_business_verification_approved_email,
    send_business_verification_rejected_email,
    send_admin_new_verification_request_email,
)
from quickstart.utils.permissions import (
    CanProcessVerificationRequests,
    CanViewAllVerificationRequests,
)


logger = logging.getLogger(__name__)
User = (
    get_user_model()
)  # This should correctly get CustomUser if AUTH_USER_MODEL is set

BUSINESS_OWNER_ROLE_NAME = "Business Owner"
# This is the status value we'll use for both VerificationRequest and BusinessInfo upon approval
VERIFIED_STATUS = "verified"  # <<<< CONSISTENT STATUS


class VerificationRequestViewSet(viewsets.ModelViewSet):
    """Viewset for managing verification requests"""

    filter_backends = [filters.SearchFilter]
    search_fields = [
        "user__email",
        "user__first_name",
        "user__last_name",
        "business__businessName",
    ]

    def get_permissions(self):
        if self.action in [
            "create",
            "submit_verification",
        ]:  # User submitting their own
            return [IsAuthenticated()]
        elif self.action in ["list", "retrieve"]:  # Admin viewing
            return [IsAuthenticated(), CanViewAllVerificationRequests()]
        elif self.action in [
            "process_verification",
            "update",
            "partial_update",
            "destroy",
        ]:  # Admin processing/editing
            return [IsAuthenticated(), CanProcessVerificationRequests()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        queryset = (
            VerificationRequest.objects.all()
            .select_related(
                "user", "user__role", "business", "reviewed_by", "reviewed_by__role"
            )
            .prefetch_related("documents")
        )

        if not user.has_perm("quickstart.view_all_verificationrequests"):
            logger.warning(
                f"User {user.email} without 'view_all_verificationrequests' accessed get_queryset in admin VerificationRequestViewSet. Action: {self.action}"
            )
            return VerificationRequest.objects.none()

        status_filter = self.request.query_params.get("status")
        if status_filter:
            status_list = status_filter.split(",")
            valid_statuses = [s.strip() for s in status_list if s.strip()]
            if valid_statuses:
                queryset = queryset.filter(status__in=valid_statuses)
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return VerificationRequestListSerializer
        elif self.action == "create" or self.action == "submit_verification":
            return VerificationSubmissionSerializer
        elif self.action == "process_verification":
            return VerificationProcessSerializer
        return VerificationRequestDetailSerializer

    @action(detail=False, methods=["post"], permission_classes=[IsAuthenticated])
    def submit_verification(self, request):
        """Endpoint for users to submit verification requests"""
        serializer = VerificationSubmissionSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        verification = serializer.save()

        try:
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action="verification_submit",
                details=f"Verification request submitted for business ID {verification.business.businessId if verification.business else 'N/A'}",
                target_model="VerificationRequest",
                target_id=str(verification.id),
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception as e:
            logger.error(
                f"Failed to create audit log for verification submission: {str(e)}"
            )

        try:
            content_type = ContentType.objects.get_for_model(VerificationRequest)
            admin_perm_codename = "process_verificationrequest"
            admin_perm = Permission.objects.get(
                content_type=content_type, codename=admin_perm_codename
            )

            admin_users = (
                User.objects.filter(
                    Q(is_superuser=True)
                    | Q(groups__permissions=admin_perm)
                    | Q(user_permissions=admin_perm)
                )
                .filter(is_active=True, email__isnull=False)
                .exclude(email="")
                .distinct()
            )
            admin_emails = list(admin_users.values_list("email", flat=True))

            if admin_emails:
                send_admin_new_verification_request_email(admin_emails, verification)
                logger.info(
                    f"Admin notification queued for new verification request {verification.id} to {len(admin_emails)} admins."
                )
            else:
                logger.warning(
                    f"No active admin users found with '{admin_perm_codename}' permission to notify about verification {verification.id}"
                )

        except Permission.DoesNotExist:
            logger.error(
                f"Permission '{admin_perm_codename}' not found. Cannot notify admins about new verification request."
            )
        except Exception as e:
            logger.error(
                f"Failed to send admin notification email for new verification {verification.id}: {e}",
                exc_info=True,
            )

        return Response(
            VerificationRequestDetailSerializer(
                verification, context={"request": request}
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def process_verification(self, request, pk=None):
        """Endpoint for admins to approve or reject verification requests"""
        verification = self.get_object()

        if verification.status != "pending":
            return Response(
                {"detail": "This verification request has already been processed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        validated_data = serializer.validated_data
        action_input = validated_data.get("status")  # Will be 'approved' or 'rejected'
        notes = validated_data.get("notes", "")
        rejection_reason = validated_data.get(
            "rejection_reason", ""
        )  # Now correctly uses the validated field

        target_db_status = None
        if action_input == "approved":  # CORRECTED: Was 'approve'
            target_db_status = (
                VERIFIED_STATUS  # This correctly uses the 'verified' constant
            )
        elif action_input == "rejected":  # CORRECTED: Was 'reject'
            target_db_status = "rejected"

        if target_db_status is None:
            logger.error(
                f"Internal logic error: Validated action '{action_input}' did not map to a DB status for verification {pk}."
            )
            return Response(
                {"error": "Internal processing error."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # --- Update the VerificationRequest model instance ---
        # The VerificationRequest.save() method will handle syncing `status` to `BusinessInfo.verificationStatus`
        verification.status = target_db_status
        verification.notes = notes
        verification.rejection_reason = (
            rejection_reason  # Use the correct, validated reason
        )
        verification.reviewed_by = request.user
        verification.reviewed_at = timezone.now()
        verification.save()  # This triggers the sync in VerificationRequest.save()

        # --- Post-save actions (Logging, Email, Role Change, Business Activation) ---
        action_code = None
        log_details = f"Verification request {verification.status}"
        if (
            verification.status == VERIFIED_STATUS
        ):  # Check against consistent 'verified'
            action_code = "verification_approve"
            try:
                if verification.user and verification.business:
                    send_business_verification_approved_email(
                        user=verification.user, business=verification.business
                    )
                    logger.info(
                        f"Verification '{VERIFIED_STATUS}' email prepared for business {verification.business.businessId}, user {verification.user.email}"
                    )

                    try:
                        target_role = Role.objects.get(name=BUSINESS_OWNER_ROLE_NAME)
                        current_role = verification.user.role

                        # Determine if we should assign the new role
                        should_assign_role = False
                        if current_role is None:
                            # If user has no role, assign the Business Owner role
                            should_assign_role = True
                            logger.info(
                                f"User {verification.user.email} has no current role. Assigning '{target_role.name}'."
                            )
                        elif target_role.hierarchy_level > current_role.hierarchy_level:
                            # Only assign if the Business Owner role is a promotion
                            should_assign_role = True
                            logger.info(
                                f"Upgrading user {verification.user.email} from '{current_role.name}' (level {current_role.hierarchy_level}) to '{target_role.name}' (level {target_role.hierarchy_level})."
                            )
                        else:
                            # Do not demote or change role if current role is of equal or higher hierarchy
                            logger.info(
                                f"User {verification.user.email} retains current role '{current_role.name}' (level {current_role.hierarchy_level}) as it is not lower than '{target_role.name}'. No role change."
                            )

                        if should_assign_role:
                            original_role_name = (
                                current_role.name if current_role else "None"
                            )
                            verification.user.role = target_role
                            verification.user.save(update_fields=["role"])
                            logger.info(
                                f"User {verification.user.email}'s role successfully changed to '{target_role.name}'."
                            )

                            try:
                                AuditLog.objects.create(
                                    user=request.user,
                                    user_email=request.user.email,
                                    action="role_change",
                                    details=f"User {verification.user.email} role changed to '{target_role.name}' due to business verification.",
                                    target_user=verification.user,
                                    target_model="CustomUser",
                                    target_id=str(verification.user.userId),
                                    ip_address=request.META.get("REMOTE_ADDR"),
                                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                                    metadata={
                                        "verification_request_id": str(verification.id),
                                        "previous_role": original_role_name,
                                        "new_role": target_role.name,
                                    },
                                )
                            except Exception as audit_e:
                                logger.error(
                                    f"Failed to create audit log for role change (user {verification.user.email}): {audit_e}"
                                )

                    except Role.DoesNotExist:
                        logger.error(
                            f"CRITICAL: Role '{BUSINESS_OWNER_ROLE_NAME}' not found. Cannot assign role to user {verification.user.email}."
                        )
                    except Exception as role_change_error:
                        logger.error(
                            f"Failed to change role for user {verification.user.email}: {role_change_error}",
                            exc_info=True,
                        )

                    if not verification.business.isActive:
                        verification.business.isActive = True
                        verification.business.save(update_fields=["isActive"])
                        logger.info(
                            f"Business {verification.business.businessId} automatically set to active upon verification."
                        )
                    else:
                        logger.info(
                            f"Business {verification.business.businessId} was already active."
                        )

                else:
                    logger.error(
                        f"Cannot process approval actions for verification {verification.id}: Missing user or business link."
                    )
            except (
                Exception
            ) as approval_action_error:  # Catch-all for errors during approval post-actions
                logger.error(
                    f"Error during post-approval actions for verification {verification.id}: {approval_action_error}",
                    exc_info=True,
                )

        elif verification.status == "rejected":
            action_code = "verification_reject"
            log_details += (
                f". Reason: {verification.rejection_reason or 'None provided'}"
            )
            try:
                if verification.user and verification.business:
                    send_business_verification_rejected_email(
                        user=verification.user,
                        business=verification.business,
                        verification_request=verification,
                    )
                    logger.info(
                        f"Verification rejected email prepared for business {verification.business.businessId}, user {verification.user.email}"
                    )
                else:
                    logger.error(
                        f"Cannot send rejection email for verification {verification.id}: Missing user or business link."
                    )
            except Exception as email_error:
                logger.error(
                    f"Failed to send verification rejected email for {verification.id}: {email_error}",
                    exc_info=True,
                )

        if action_code:
            try:
                AuditLog.objects.create(
                    user=request.user,
                    user_email=request.user.email,
                    action=action_code,
                    details=log_details,
                    target_user=verification.user,
                    target_model="VerificationRequest",
                    target_id=str(verification.id),
                    ip_address=request.META.get("REMOTE_ADDR"),
                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                    metadata={
                        "business_id": (
                            verification.business.businessId
                            if verification.business
                            else None
                        ),
                        "business_name": (
                            verification.business.businessName
                            if verification.business
                            else None
                        ),
                        "notes": verification.notes,
                        "rejection_reason": (
                            verification.rejection_reason
                            if verification.status == "rejected"
                            else None
                        ),
                    },
                )
            except Exception as e:
                logger.error(
                    f"Failed to create audit log for verification processing: {str(e)}"
                )

        return Response(
            VerificationRequestDetailSerializer(
                verification, context={"request": request}
            ).data
        )

    @action(detail=True, methods=["get"])
    def documents(self, request, pk=None):
        verification = self.get_object()
        documents = verification.documents.all()
        serializer = VerificationDocumentSerializer(
            documents, many=True, context={"request": request}
        )
        return Response(serializer.data)
