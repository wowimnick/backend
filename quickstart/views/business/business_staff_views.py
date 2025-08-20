# quickstart/views/business/business_staff_views.py

from rest_framework import viewsets, status, generics
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import (
    PermissionDenied,
    ValidationError as DRFValidationError,
    NotFound,
)
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
import logging

from quickstart.models import (
    BusinessInfo,
    BusinessStaff,
    CustomUser,
    Role,
    BusinessRole,
)
from quickstart.serializers import (
    BusinessStaffSerializer,
    StaffInviteSerializer,
    InvitationDetailsSerializer,
    CustomUserDetailsSerializer,
)
from quickstart.utils.permissions import (
    IsBusinessOwnerOrManager,
)
from quickstart.utils.email_utils import send_business_staff_invitation_email

logger = logging.getLogger(__name__)


class BusinessStaffViewSet(viewsets.ModelViewSet):
    serializer_class = BusinessStaffSerializer
    permission_classes = [
        IsAuthenticated,
        IsBusinessOwnerOrManager,
    ]

    def get_queryset(self):
        user = self.request.user
        business = (
            BusinessInfo.objects.filter(Q(owner=user) | Q(staff_members__user=user))
            .distinct()
            .first()
        )
        if not business:
            return BusinessStaff.objects.none()
        return BusinessStaff.objects.filter(business=business).select_related(
            "user", "role"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return StaffInviteSerializer
        return BusinessStaffSerializer

    def perform_create(self, serializer):
        user = self.request.user
        business = (
            BusinessInfo.objects.filter(Q(owner=user) | Q(staff_members__user=user))
            .distinct()
            .first()
        )

        if not business:
            raise PermissionDenied("You are not associated with any business.")

        is_owner = business.owner == user
        inviter_staff_profile = BusinessStaff.objects.filter(
            user=user, business=business
        ).first()

        can_manage_staff = False
        if is_owner:
            can_manage_staff = True
        elif inviter_staff_profile and inviter_staff_profile.role:
            can_manage_staff = inviter_staff_profile.role.permissions.filter(
                codename="manage_business_staff"
            ).exists()

        if not can_manage_staff:
            raise PermissionDenied(
                "You do not have permission to invite staff members."
            )

        invited_email = serializer.validated_data["invited_email"]
        role_to_assign = serializer.validated_data["role"]

        # Ensure the role belongs to the correct business
        if role_to_assign.business != business:
            raise DRFValidationError(
                {"role": "This role is not valid for your business."}
            )

        # Check if user already exists or is already invited
        if CustomUser.objects.filter(
            email=invited_email, staff_roles__business=business
        ).exists():
            raise DRFValidationError(
                {
                    "invited_email": "This user is already a member of your business staff."
                }
            )
        if BusinessStaff.objects.filter(
            business=business, invited_email=invited_email, status="pending"
        ).exists():
            raise DRFValidationError(
                {
                    "invited_email": "An invitation has already been sent to this email address."
                }
            )

        # Create the invitation
        instance = serializer.save(business=business, invited_by=user, status="pending")

        # Send the invitation email
        send_business_staff_invitation_email(instance)
        logger.info(
            f"Staff invitation sent to {invited_email} for business '{business.businessName}' by {user.email}"
        )

    def perform_update(self, serializer):
        # This view only allows updating the 'role'
        instance = self.get_object()
        user = self.request.user

        # You cannot change your own role.
        if instance.user == user:
            raise PermissionDenied("You cannot change your own role.")

        # The owner's role cannot be changed.
        if instance.user == instance.business.owner:
            raise PermissionDenied("The business owner's role cannot be changed.")

        serializer.save()


# --- View to accept the invitation ---
class AcceptStaffInvitationView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        token = request.data.get("token")
        if not token:
            raise DRFValidationError({"token": "Invitation token is required."})

        # Try to find the invitation
        try:
            invitation = BusinessStaff.objects.get(
                invitation_token=token, status="pending"
            )
        except BusinessStaff.DoesNotExist:
            # NEW: Check if this user has ALREADY accepted this invitation.
            # This makes the endpoint idempotent.
            already_accepted = BusinessStaff.objects.filter(
                user=request.user,
                invitation_token=None,
                status="accepted",
                # You might need a way to link back to the original token if needed,
                # but checking against the user is a strong indicator.
            ).exists()

            if BusinessStaff.objects.filter(
                user=request.user, business__staff_members__invitation_token=token
            ).exists():
                # User is already a member of the business they are trying to join.
                # This can happen if the useEffect runs twice.
                logger.warning(
                    f"User {request.user.email} attempted to accept an invitation they have already joined."
                )
                # Return the updated user object just like a successful new acceptance.
                serializer = CustomUserDetailsSerializer(request.user)
                return Response(
                    {
                        "success": True,
                        "message": "You are already a member of this team.",
                        "user": serializer.data,
                    },
                    status=status.HTTP_200_OK,
                )

            raise NotFound(
                "This invitation is invalid, expired, or has already been used."
            )

        if request.user.email.lower() != invitation.invited_email.lower():
            raise PermissionDenied(
                "This invitation is intended for a different email address."
            )

        if (
            BusinessStaff.objects.filter(user=request.user, status="accepted")
            .exclude(id=invitation.id)
            .exists()
        ):
            raise PermissionDenied("You are already a member of another business team.")

        invitation.user = request.user
        invitation.status = "accepted"
        invitation.invitation_token = None
        invitation.save()

        logger.info(
            f"User {request.user.email} accepted invitation to join '{invitation.business.businessName}'"
        )

        # --- CRITICAL FIX ---
        # You MUST return the updated user object in the response so the Redux slice can update the permissions.
        serializer = CustomUserDetailsSerializer(
            request.user
        )  # Use your existing user serializer

        return Response(
            {
                "success": True,
                "message": f"You have successfully joined {invitation.business.businessName}.",
                "user": serializer.data,  # Add the user object here
            },
            status=status.HTTP_200_OK,
        )


class ValidateInvitationTokenView(generics.GenericAPIView):
    """
    Publicly validates a staff invitation token and returns its details.
    """

    permission_classes = [AllowAny]  # This endpoint must be public
    serializer_class = InvitationDetailsSerializer

    def get(self, request, *args, **kwargs):
        token = request.query_params.get("token")
        if not token:
            raise DRFValidationError({"token": "Invitation token is required."})

        try:
            # We only look for pending invitations. Accepted or expired ones are invalid.
            invitation = BusinessStaff.objects.get(
                invitation_token=token, status="pending"
            )
        except BusinessStaff.DoesNotExist:
            raise NotFound("This invitation is invalid or has already been accepted.")

        serializer = self.get_serializer(instance=invitation)
        return Response(serializer.data, status=status.HTTP_200_OK)
