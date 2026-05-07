# quickstart/utils/permissions.py
"""
Centralized permission classes and DRF re-exports for the quickstart app.
Import IsAuthenticated, AllowAny, BasePermission and any custom permission from here.
"""

from rest_framework.permissions import (
    BasePermission,
    IsAuthenticated,
    AllowAny,
)
from django.db.models import Q
import logging

from quickstart.models import (
    Booking,
    BusinessInfo,
    Discount,
    BusinessStaff,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helper for admin hierarchy (used by CanManageTargetUser, CanManageTargetBooking, etc.)
# ---------------------------------------------------------------------------


def user_can_manage(requesting_user, target_user):
    """Return True if requesting_user can manage target_user based on role hierarchy."""
    if not requesting_user or not target_user:
        return False
    if not requesting_user.is_authenticated:
        return False
    if requesting_user.is_superuser or (
        requesting_user.role and requesting_user.role.name == "Super Admin"
    ):
        return True
    if not target_user.role:
        return True
    if not requesting_user.role:
        return False
    return requesting_user.role.hierarchy_level > target_user.role.hierarchy_level


# ---------------------------------------------------------------------------
# Admin permissions
# ---------------------------------------------------------------------------


class CanAccessUserAdmin(BasePermission):
    message = "You do not have permission to access user administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_user_admin")

class IsWidgetRequest(BasePermission):
    """
    Checks if a request is from the widget by validating the X-Business-ID header.
    If valid, it attaches the business object to the request for easy access in views.
    """

    message = "Invalid or missing Business ID."

    def has_permission(self, request, view):
        # The widget_api_key (UUID) will be sent in this header
        business_key = request.headers.get("X-Business-ID")
        if not business_key:
            return False

        try:
            # Find the business that is active and verified
            business = BusinessInfo.objects.get(
                widget_api_key=business_key,
                isActive=True,
                verificationStatus="verified",
            )
            # Attach the business context to the request for the view to use
            request.business_context = business
            return True
        except (BusinessInfo.DoesNotExist, ValueError):
            return False


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


class CanManageEmailMarketing(BasePermission):
    """
    Allows access if user has 'manage_email_marketing' OR 'manage_own_classes'
    permission (the latter is the legacy gate; the new codename allows granting
    email marketing access independently of class management).
    """

    message = "You do not have permission to manage email marketing for this business."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return user.has_perm("quickstart.manage_email_marketing") or user.has_perm(
            "quickstart.manage_own_classes"
        )


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


class CanManageOwnClassesOrClassAdmin(BasePermission):
    """
    Business dashboard schedule APIs: business members, or platform class admins
    (same /business/schedules/ URLs — no impersonation required).
    """

    message = "You do not have permission to manage schedules."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.has_perm("quickstart.access_class_admin"):
            return True
        return user.has_perm("quickstart.manage_own_classes")

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.has_perm("quickstart.access_class_admin"):
            return True
        return CanManageOwnClasses().has_object_permission(request, view, obj)


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
    Allows access only if the user is the owner or an accepted staff member of the related BusinessInfo.

    Works for views whose object is BusinessInfo (e.g. MyBusinessProfileView) or a model with a `business`
    FK to BusinessInfo (e.g. BusinessLocation on PATCH/DELETE detail).
    """

    message = "You do not have permission to manage this business profile."

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        if isinstance(obj, BusinessInfo):
            business = obj
        else:
            related = getattr(obj, "business", None)
            business = related if isinstance(related, BusinessInfo) else None
        if business is None:
            return False

        user = request.user
        is_owner = business.owner == user
        is_staff = business.staff_members.filter(user=user, status="accepted").exists()
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


# --- Admin: User management ---
class CanImpersonateUser(BasePermission):
    message = "Only Super Admins can impersonate users."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and hasattr(request.user, "role")
            and request.user.role is not None
            and request.user.role.name == "Super Admin"
        )


class CanManageTargetUser(BasePermission):
    message = "You cannot manage this user due to hierarchy restrictions."

    def has_object_permission(self, request, view, obj):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return user_can_manage(request.user, obj)


# --- Admin: Business management ---
class CanAccessBusinessAdmin(BasePermission):
    message = "You do not have permission to access business administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_business_admin")


class CanManageTargetBusiness(BasePermission):
    message = "You cannot manage this business due to hierarchy or ownership restrictions."

    def has_object_permission(self, request, view, obj):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        if not hasattr(obj, "owner") or not obj.owner:
            logger.warning(
                f"BusinessInfo object (ID: {obj.pk}) is missing an owner. Denying management access."
            )
            return False
        return user_can_manage(request.user, obj.owner)


# --- Admin: Class / Category / Review ---
class CanAccessClassAdmin(BasePermission):
    message = "You do not have permission to access class administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_class_admin")


class CanAccessCategoryAdmin(BasePermission):
    message = "You do not have permission to access category administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_category_admin")


class CanAccessReviewAdmin(BasePermission):
    message = "You do not have permission to access review moderation."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_review_admin")


# --- Admin: Support ---
class CanAccessSupportAdmin(BasePermission):
    message = "You do not have permission to access support ticket administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_support_admin")


# --- Admin: Booking / Payment / Payout ---
class CanAccessBookingAdmin(BasePermission):
    message = "You do not have permission to access booking administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_booking_admin")


class CanManageTargetBooking(BasePermission):
    message = "You cannot manage this booking due to hierarchy restrictions."

    def has_object_permission(self, request, view, obj):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        # Guest bookings have no user; allow admins who passed has_permission (e.g. access_booking_admin)
        if obj.user is None:
            return True
        return user_can_manage(request.user, obj.user)


class CanAccessPaymentAdmin(BasePermission):
    message = "You do not have permission to access payment administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_payment_admin")


class CanManageTargetPayment(BasePermission):
    message = "You cannot manage this payment due to hierarchy restrictions."

    def has_object_permission(self, request, view, obj):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        if not obj.booking or not obj.booking.user:
            return True
        return user_can_manage(request.user, obj.booking.user)


class CanAccessPayoutAdmin(BasePermission):
    message = "You do not have permission to access payout administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_payout_admin")


# --- Admin: Blog ---
class CanAccessBlogAdmin(BasePermission):
    message = "You do not have permission to access blog administration."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("quickstart.access_blog_admin")
        )


# --- Admin: Global Discount ---
class CanAccessGlobalDiscountAdmin(BasePermission):
    message = "You do not have permission to access global discount management."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("quickstart.access_global_discount_admin")
        )


# --- Admin: Corporate inquiries / shortlists / bookings ---
class CanAccessCorporateAdmin(BasePermission):
    message = "You do not have permission to access corporate booking administration."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.has_perm("quickstart.access_corporate_admin")
        )


# --- Admin: Notifications ---
class CanAccessNotificationAdmin(BasePermission):
    message = "You do not have permission to access notification management."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_notification_admin")


class CanAccessSegmentAdmin(BasePermission):
    message = "You do not have permission to access user segment management."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_segment_admin")


# --- Admin: Metrics ---
class CanViewSystemMetrics(BasePermission):
    message = "You do not have permission to view system metrics."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.view_system_metrics")


# --- Business: Bookings (manage/cancel) ---
class CanManageOwnBusinessBookings(BasePermission):
    message = "You do not have permission to manage this booking."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return (
            user.has_perm("quickstart.view_own_business_bookings")
            and user.has_perm("quickstart.cancel_business_booking")
            and BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).exists()
        )

    def has_object_permission(self, request, view, obj):
        user = request.user
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            return False
        try:
            return obj.schedule_instance.schedule.option.classId.businessId == business
        except AttributeError:
            return False


# --- Business: Reviews ---
class CanManageOwnBusinessReviews(BasePermission):
    message = "You do not have permission to manage reviews for this business."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return (
            user.has_perm("quickstart.view_own_business_reviews")
            and BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).exists()
        )

    def has_object_permission(self, request, view, obj):
        user = request.user
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            return False
        return obj.classId and obj.classId.businessId == business


# --- Business: Students ---
class CanViewBusinessStudents(BasePermission):
    message = "You do not have permission to view students for this business."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return (
            user.has_perm("quickstart.view_business_students")
            and BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).exists()
        )


class CanManageBusinessStudentNotes(BasePermission):
    message = "You do not have permission to manage notes for this student."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return user.has_perm("quickstart.view_studentnote") or user.has_perm(
            "quickstart.add_studentnote"
        )


# --- Widget ---
class IsValidWidgetRequest(BasePermission):
    """Checks that request.business_context is set and valid (use after middleware). Allows demo sentinel."""

    message = "Invalid or missing Business ID."

    def has_permission(self, request, view):
        business = getattr(request, "business_context", None)
        if not business:
            return False
        if getattr(business, "is_demo", False):
            return True
        return (
            business.isActive
            and business.verificationStatus == "verified"
        )


# --- Verification (admin) ---
class CanViewAllVerificationRequests(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.has_perm("quickstart.view_all_verificationrequests")


class CanProcessVerificationRequests(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.has_perm("quickstart.process_verificationrequest")
