# quickstart/utils/permissions.py

from rest_framework.permissions import BasePermission
from django.db.models import Q
import logging

from quickstart.models import (
    Booking,
    BusinessInfo,
    Discount,
    BusinessStaff,
)  # MODIFIED: Import BusinessStaff

logger = logging.getLogger(__name__)


# MODIFIED: Renamed class for clarity and updated logic.
class IsBusinessMember(BasePermission):
    """
    Allows access only to users who are the owner of a business or an accepted staff member.
    This is a general, view-level permission check.
    """

    message = "You must be a business owner or an active staff member to access this resource."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False

        # MODIFIED: The key check now includes staff_members.
        # Does a business exist where this user is the owner OR an accepted staff member?
        return BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).exists()


class CanManageOwnClasses(BasePermission):
    """
    Allows access if user has 'manage_own_classes' permission AND
    the class/option/schedule/instance object belongs to their associated business.
    """

    message = (
        "You do not have permission to manage classes/schedules for this business."
    )

    def has_permission(self, request, view):
        user = request.user
        return (
            user
            and user.is_authenticated
            and user.has_perm("quickstart.manage_own_classes")
        )

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not self.has_permission(request, view):
            return False

        # MODIFIED: Find the business associated with the user via ownership OR staff membership.
        user_business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()

        if not user_business:
            logger.warning(
                f"Permission check failed for user {user.email}: User has 'manage_own_classes' perm but no associated business."
            )
            return False

        object_business = None
        try:
            if hasattr(obj, "businessId"):
                object_business = obj.businessId
            elif hasattr(obj, "classId") and hasattr(obj.classId, "businessId"):
                object_business = obj.classId.businessId
            elif hasattr(obj, "schedule") and hasattr(obj.schedule, "option"):
                object_business = obj.schedule.option.classId.businessId
            elif hasattr(obj, "option") and hasattr(obj.option, "classId"):
                object_business = obj.option.classId.businessId

            if not object_business:
                logger.error(
                    f"Could not determine business association for object type {type(obj)} with pk {getattr(obj, 'pk', 'N/A')} during permission check."
                )
                return False

            is_authorized = object_business == user_business
            if not is_authorized:
                logger.warning(
                    f"Permission denied for user {user.email} on object {obj} (Business: {object_business.businessId}). User is member of Business: {user_business.businessId}."
                )
            return is_authorized

        except AttributeError as e:
            logger.error(
                f"AttributeError during object permission check for user {user.email} on object {obj}: {e}",
                exc_info=True,
            )
            return False


# MODIFIED: This permission class is being deprecated by the more specific IsBusinessMember
# but we update it for any legacy use cases. Best practice would be to phase it out.
class IsBusinessOwnerOrManager(BasePermission):
    """
    DEPRECATED: Use IsBusinessMember instead.
    Allows access only to users who are registered as an owner or an accepted staff member.
    """

    message = "You must be a business owner or manager to access this resource."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        # MODIFIED: The key check now includes staff_members.
        return BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).exists()


# --- Other permission classes that need updating ---


class CanAccessBusinessDashboard(BasePermission):
    """Allows access if user has 'access_business_dashboard' permission AND is a member of the business."""

    message = "You do not have permission to access this business dashboard."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("quickstart.access_business_dashboard")
        )

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not self.has_permission(request, view):
            return False

        # MODIFIED: Check ownership OR staff membership for the specific business object.
        is_owner = obj.owner == user
        is_staff = obj.staff_members.filter(user=user, status="accepted").exists()
        can_access = is_owner or is_staff

        if not can_access:
            logger.warning(
                f"Dashboard access denied for user {user.email} on business {obj.businessId}. Not owner or staff."
            )
        return can_access


class CanManageOwnBusinessProfile(BasePermission):
    """
    Allows access only if the user is the owner or an accepted staff member of the specific BusinessInfo object.
    """

    message = "You do not have permission to manage this business profile."

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        if not isinstance(obj, BusinessInfo):
            return False

        user = request.user
        # MODIFIED: Check ownership OR if user is an accepted staff member.
        is_owner = obj.owner == user
        is_staff = obj.staff_members.filter(user=user, status="accepted").exists()
        return is_owner or is_staff


class CanDeleteOwnBusinessProfile(BasePermission):
    """Allows access only if the user is the OWNER of the BusinessInfo object."""

    message = "Only the business owner can delete the profile."

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        if not isinstance(obj, BusinessInfo):
            return False
        # Only the owner can delete, this logic remains correct.
        return obj.owner == request.user


class CanViewOwnBusinessBookings(BasePermission):
    """
    Allows access if user has 'view_own_business_bookings' permission AND
    the booking(s) belong to their associated business.
    """

    message = "You do not have permission to view bookings for this business."

    def has_permission(self, request, view):
        """
        Checks for the specific permission codename and general business membership.
        This protects the LIST view.
        """
        user = request.user
        if not (user and user.is_authenticated):
            return False

        has_codename = user.has_perm("quickstart.view_own_business_bookings")
        is_member = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).exists()

        return has_codename and is_member

    def has_object_permission(self, request, view, obj):
        """
        Checks if the specific booking object belongs to the user's business.
        This protects the DETAIL view.
        """
        user = request.user

        # Find the business associated with the user.
        user_business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()

        if not user_business:
            return False  # Should be caught by has_permission, but safe to have.

        # Determine the business from the booking object.
        if isinstance(obj, Booking):
            try:
                object_business = (
                    obj.schedule_instance.schedule.option.classId.businessId
                )
                return object_business == user_business
            except AttributeError:
                # This can happen if a booking's related objects are deleted.
                return False

        return False


class IsVerifiedAndActiveBusinessMember(BasePermission):  # MODIFIED: Renamed class
    message = "Your business account is not active or verified."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        # MODIFIED: Check if user is associated with *any* business (as owner or staff) that is active and verified.
        return BusinessInfo.objects.filter(
            (
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ),
            isActive=True,
            verificationStatus="verified",
        ).exists()

    def has_object_permission(self, request, view, obj):
        user = request.user
        business = None

        if isinstance(obj, BusinessInfo):
            business = obj
        elif isinstance(obj, Discount):
            business = obj.business
        elif hasattr(obj, "businessId"):
            business = obj.businessId
        elif hasattr(obj, "classId"):
            business = obj.classId.businessId
        elif hasattr(obj, "schedule_instance"):
            try:
                business = obj.schedule_instance.schedule.option.classId.businessId
            except AttributeError:
                return False

        if not business:
            return False

        # MODIFIED: Check ownership/staff status AND active/verified status.
        is_owner = business.owner == user
        is_staff = business.staff_members.filter(user=user, status="accepted").exists()

        is_authorized = (
            (is_owner or is_staff)
            and business.isActive is True
            and business.verificationStatus == "verified"
        )

        if not is_authorized:
            logger.warning(
                f"User {user.email} permission failed for business {business.pk}. IsOwner/Staff: {is_owner or is_staff}, IsActive: {business.isActive}, Verification: {business.verificationStatus}"
            )

        return is_authorized


# --- Admin-level permissions remain unchanged ---
class CanViewAllVerificationRequests(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("quickstart.view_all_verificationrequests")


class CanProcessVerificationRequests(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("quickstart.process_verificationrequest")
