from datetime import timedelta
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import (
    IsAuthenticated,
    BasePermission,
)  # Import BasePermission
from rest_framework.pagination import PageNumberPagination
from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone
import logging

from quickstart.models import AuditLog, Role, Booking  # Import Role
from quickstart.serializers.admin.user_management.admin_serializers import (
    AdminUserListSerializer,
    AdminUserDetailSerializer,
    AdminUserCreateUpdateSerializer,
    AdminUserBookingSerializer,
)

User = get_user_model()
logger = logging.getLogger(__name__)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


# --- Permission Helper Functions ---
def user_can_manage(requesting_user, target_user):
    """
    Checks if the requesting user can manage the target user.
    A superuser or a user with the 'Super Admin' role bypasses all hierarchy checks.
    """
    if not requesting_user or not target_user:
        return False
    if not requesting_user.is_authenticated:
        return False  # Must be logged in

    # Superuser/Super Admin override: can manage anyone.
    if requesting_user.is_superuser or (
        requesting_user.role and requesting_user.role.name == "Super Admin"
    ):
        return True

    # Allow managing users without roles (e.g., newly created)
    if not target_user.role:
        return True

    # Deny if requester has no role (and is not a superuser)
    if not requesting_user.role:
        return False

    # For non-super admins, check the hierarchy level
    return requesting_user.role.hierarchy_level > target_user.role.hierarchy_level


# --- Custom Permission Classes ---


class CanAccessUserAdmin(BasePermission):
    """Allows access only to users with 'access_user_admin' permission."""

    message = "You do not have permission to access user administration."  # Add custom message

    def has_permission(self, request, view):
        # Ensure user is authenticated and active before checking permissions
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm("quickstart.access_user_admin")


class CanManageTargetUser(BasePermission):
    """Checks if the requesting user can manage the target user based on hierarchy."""

    message = "You cannot manage this user due to hierarchy restrictions."  # Add custom message

    def has_object_permission(self, request, view, obj):
        # 'obj' is the target User instance
        # Ensure user is authenticated and active
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return user_can_manage(request.user, obj)


# --- ViewSet ---


class UserAdminViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing users (Uses Django Permissions & Hierarchy)
    """

    pagination_class = StandardResultsSetPagination

    # Base permission: Must be authenticated and have general access
    permission_classes = [IsAuthenticated, CanAccessUserAdmin]
    filter_backends = [
        filters.SearchFilter,
        filters.OrderingFilter,
    ]  # Added OrderingFilter
    search_fields = ["email", "first_name", "last_name", "phone_number"]
    ordering_fields = [
        "first_name",
        "last_name",
        "email",
        "createdAt",
        "last_login",
        "bookings_count",
    ]  # Define fields for ordering
    ordering = ["-createdAt"]

    def get_queryset(self):
        queryset = (
            User.objects.select_related("role")
            .prefetch_related("owned_businesses")
            .all()
        )

        # Add role filtering
        role_id = self.request.query_params.get("role_id")  # Use ID for filtering
        if role_id:
            # Handle potential multiple role IDs if needed, e.g., role_id=1,2,3
            role_ids = [r_id for r_id in role_id.split(",") if r_id.isdigit()]
            if role_ids:
                queryset = queryset.filter(role_id__in=role_ids)

        # Add status filtering (refined logic)
        status_filter = self.request.query_params.get("status")
        if status_filter == "active":
            # Active: is_active=True AND has logged in before
            queryset = queryset.filter(is_active=True, last_login__isnull=False)
        elif status_filter == "inactive":
            # Inactive: is_active=False
            queryset = queryset.filter(is_active=False)
        elif status_filter == "pending":
            # Pending: is_active=True AND never logged in
            queryset = queryset.filter(is_active=True, last_login__isnull=True)
        # No status filter means all users regardless of active/last_login

        queryset = queryset.annotate(bookings_count=Count("bookings", distinct=True))
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return AdminUserListSerializer
        elif self.action in ["create", "update", "partial_update"]:
            return AdminUserCreateUpdateSerializer
        return AdminUserDetailSerializer

    @action(
        detail=True,
        methods=["get"],
        permission_classes=[
            IsAuthenticated,
            CanAccessUserAdmin,
        ],  # Basic admin access needed
        pagination_class=StandardResultsSetPagination,  # Apply pagination to this action
        url_path="bookings",
    )  # Sets the URL part: /admin/users/{pk}/bookings/
    def get_user_bookings(self, request, pk=None):
        """
        Retrieve the booking history for a specific user.
        """
        target_user = self.get_object()  # Gets the user based on pk

        # Check if the requesting user *can* view the target user's details (optional hierarchy check)
        # if not user_can_manage(request.user, target_user):
        #     self.permission_denied(request, message="You cannot view this user's bookings due to hierarchy.")

        # Fetch bookings, prefetch related data for efficiency
        user_bookings = (
            Booking.objects.filter(user=target_user)
            .select_related(
                "schedule_instance__schedule__option__classId"  # Prefetch necessary related models
            )
            .order_by("-booking_date")
        )  # Order by most recent booking

        # Apply pagination
        page = self.paginate_queryset(user_bookings)
        if page is not None:
            serializer = AdminUserBookingSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)

        # Fallback if pagination is not enabled or used
        serializer = AdminUserBookingSerializer(
            user_bookings, many=True, context={"request": request}
        )
        return Response(serializer.data)

    # --- Action Permissions and Hierarchy Checks ---

    def create(self, request, *args, **kwargs):
        # Check permission for adding users
        if not request.user.has_perm("quickstart.add_customuser"):
            # Use standard DRF permission denied response
            self.permission_denied(
                request, message="You do not have permission to add users."
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Determine if the requester has super admin privileges
        is_super_admin_request = request.user.is_superuser or (
            request.user.role and request.user.role.name == "Super Admin"
        )

        # Hierarchy Check: Can the requester assign the requested role?
        role_id = request.data.get("role")
        target_role = None
        if role_id and not is_super_admin_request:  # Bypass check for super admins
            try:
                target_role = Role.objects.get(pk=role_id)
                # Prevent non-super admins from assigning a role higher than or equal to their own
                if (
                    request.user.role
                    and request.user.role.hierarchy_level <= target_role.hierarchy_level
                ):
                    self.permission_denied(
                        request,
                        message=f"You cannot assign the role '{target_role.name}' due to hierarchy restrictions.",
                    )
            except Role.DoesNotExist:
                return Response(
                    {"detail": "Invalid role specified."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Note: serializer.save() will internally call the serializer's create method
        user = serializer.save()
        self._log_user_action(user, "user_create", "User created by admin")
        headers = self.get_success_headers(AdminUserDetailSerializer(user).data)
        return Response(
            AdminUserDetailSerializer(user).data,
            status=status.HTTP_21_CREATED,
            headers=headers,
        )

    def retrieve(self, request, *args, **kwargs):
        # Check base view permission
        if not request.user.has_perm("quickstart.view_customuser"):
            self.permission_denied(
                request, message="You do not have permission to view users."
            )
        # No hierarchy check for retrieve typically, unless profile data is very sensitive
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", True)  # Default to partial update
        instance = self.get_object()  # Target user

        # Permission Check: Need 'change_customuser' for profile fields
        has_profile_change_perm = request.user.has_perm("quickstart.change_customuser")
        has_role_change_perm = request.user.has_perm("quickstart.change_user_role")

        is_changing_profile = any(
            field in request.data
            for field in [
                "first_name",
                "last_name",
                "email",
                "birth_date",
                "bio",
                "phone_number",
                "country",
                "city",
                "state",
                "address",
                "zipCode",
                "avatar",
            ]
        )
        is_changing_role = "role" in request.data and str(
            request.data.get("role")
        ) != str(instance.role_id)

        # Deny if trying to change something without permission
        if is_changing_profile and not has_profile_change_perm:
            self.permission_denied(
                request,
                message="You do not have permission to update user profile fields.",
            )
        if is_changing_role and not has_role_change_perm:
            self.permission_denied(
                request, message="You do not have permission to change user roles."
            )
        # Deny if request is empty or only contains non-updatable fields for which user lacks permission
        if not is_changing_profile and not is_changing_role and request.data:
            # Check if any data was actually sent that requires permission
            # This logic might need refinement based on exact fields
            pass  # Allow if data is present but doesn't require specific perms checked above

        # Hierarchy Check 1: Can requester manage the target user at all?
        if not user_can_manage(request.user, instance):
            self.permission_denied(
                request,
                message="You cannot manage this user due to hierarchy restrictions.",
            )

        # Determine if the requester has super admin privileges
        is_super_admin_request = request.user.is_superuser or (
            request.user.role and request.user.role.name == "Super Admin"
        )

        # Hierarchy Check 2: If changing role, can the requester assign the NEW role?
        new_role = None
        if is_changing_role and not is_super_admin_request:  # Bypass for super admins
            new_role_id = request.data.get("role")
            if new_role_id:
                try:
                    new_role = Role.objects.get(pk=new_role_id)
                    # Prevent non-super admins from assigning a role higher than or equal to their own
                    if (
                        request.user.role
                        and request.user.role.hierarchy_level
                        <= new_role.hierarchy_level
                    ):
                        self.permission_denied(
                            request,
                            message=f"You cannot assign the role '{new_role.name}' due to hierarchy restrictions.",
                        )
                except Role.DoesNotExist:
                    return Response(
                        {"detail": "Invalid new role specified."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            else:  # Trying to remove role? Check if allowed. Usually needs specific logic.
                pass  # Allow removing role if desired, might need hierarchy check too

        # Proceed with update
        old_role_name = instance.role.name if instance.role else "None"
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()  # Serializer's update method is called
        new_role_name = user.role.name if user.role else "None"

        # Logging
        log_details = []
        if is_changing_role:
            log_details.append(f"Role changed from {old_role_name} to {new_role_name}")
            self._log_user_action(
                user,
                "role_change",
                f"Role changed from {old_role_name} to {new_role_name}",
                metadata={
                    "changes": {"role": {"from": old_role_name, "to": new_role_name}}
                },
            )
        if is_changing_profile:
            log_details.append("Profile fields updated")
            # Avoid double logging if role also changed
            if not is_changing_role:
                self._log_user_action(
                    user, "user_update", "User profile information updated by admin"
                )
        # If only password or avatar changed via serializer, log that too
        # (Requires checking serializer.validated_data vs instance before save)

        return Response(AdminUserDetailSerializer(user).data)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()  # Target user

        # Permission Check
        if not request.user.has_perm("quickstart.delete_customuser"):
            self.permission_denied(
                request, message="You do not have permission to delete users."
            )

        # Hierarchy Check
        if not user_can_manage(request.user, instance):
            self.permission_denied(
                request,
                message="You cannot delete this user due to hierarchy restrictions.",
            )
        # Prevent self-deletion for safety
        if instance == request.user:
            return Response(
                {"detail": "You cannot delete your own account via this interface."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_email = instance.email
        # Log *before* deleting
        self._log_user_action(
            instance, "user_delete", f"User {user_email} deleted by admin"
        )
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # Use permission_classes on @action for cleaner checks
    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin, CanManageTargetUser],
    )
    def lock_account(self, request, pk=None):
        user = self.get_object()  # Target user

        # Permission Check (Custom Permission)
        if not request.user.has_perm("quickstart.lock_user"):
            self.permission_denied(
                request,
                message="You do not have permission to lock/unlock user accounts.",
            )

        # Hierarchy check is handled by CanManageTargetUser permission class

        if not user.is_active:
            return Response(
                {"detail": "Account is already locked."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_active = False
        user.save(update_fields=["is_active"])  # Optimize save
        self._log_user_action(user, "account_lock", f"Account locked by admin")
        return Response({"status": "Account locked"})

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin, CanManageTargetUser],
    )
    def unlock_account(self, request, pk=None):
        user = self.get_object()  # Target user

        # Permission Check (Custom Permission) - Uses the same lock_user permission
        if not request.user.has_perm("quickstart.lock_user"):
            self.permission_denied(
                request,
                message="You do not have permission to lock/unlock user accounts.",
            )

        # Hierarchy check is handled by CanManageTargetUser permission class

        if user.is_active:
            return Response(
                {"detail": "Account is already active."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_active = True
        user.save(update_fields=["is_active"])  # Optimize save
        self._log_user_action(user, "account_unlock", f"Account unlocked by admin")
        return Response({"status": "Account unlocked"})

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin, CanManageTargetUser],
    )
    def reset_password(self, request, pk=None):
        user = self.get_object()  # Target user

        # Permission Check (Custom Permission)
        if not request.user.has_perm("quickstart.reset_user_password"):
            self.permission_denied(
                request,
                message="You do not have permission to initiate password resets.",
            )

        # Hierarchy check is handled by CanManageTargetUser permission class

        # --- Implement actual password reset token generation/email sending here ---
        # Example using Django's built-in views (requires URL setup)
        # from django.contrib.auth.forms import PasswordResetForm
        # form = PasswordResetForm({'email': user.email})
        # if form.is_valid():
        #     opts = {
        #         'use_https': request.is_secure(),
        #         'request': request,
        #         # Add other context if needed by your email templates
        #     }
        #     form.save(**opts)
        #     logger.info(f"Password reset initiated for user {user.email} by admin {request.user.email}")
        #     self._log_user_action(user, 'password_reset', f"Password reset initiated by admin")
        #     return Response({'status': 'Password reset email sent'})
        # else:
        #     logger.error(f"Password reset form invalid for user {user.email}: {form.errors}")
        #     return Response({'detail': 'Failed to initiate password reset.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Placeholder log and response:
        logger.info(
            f"Password reset initiated for user {user.email} by admin {request.user.email}"
        )
        self._log_user_action(
            user, "password_reset", f"Password reset initiated by admin"
        )
        return Response({"status": "Password reset initiated (implementation pending)"})

    @action(detail=False, methods=["get"])
    def metrics(self, request):
        # Permission Check (Custom Permission)
        if not request.user.has_perm("quickstart.view_user_metrics"):
            self.permission_denied(
                request, message="You do not have permission to view user metrics."
            )

        # --- 1. Date Range Processing ---
        try:
            # Get date range from query params, default to last 30 days
            end_date_str = request.query_params.get(
                "end_date", timezone.now().strftime("%Y-%m-%d")
            )
            default_start_date = (
                timezone.datetime.strptime(end_date_str, "%Y-%m-%d")
                - timedelta(days=29)
            ).strftime("%Y-%m-%d")
            start_date_str = request.query_params.get("start_date", default_start_date)

            # Convert to datetime objects
            start_date_dt = timezone.make_aware(
                timezone.datetime.strptime(start_date_str, "%Y-%m-%d")
            )
            end_date_dt = timezone.make_aware(
                timezone.datetime.strptime(end_date_str, "%Y-%m-%d")
            ).replace(hour=23, minute=59, second=59)
        except (ValueError, TypeError):
            # Fallback if date format is invalid
            end_date_dt = timezone.now()
            start_date_dt = end_date_dt - timedelta(days=30)

        # --- 2. User Model Aggregations ---
        user_counts = User.objects.aggregate(
            total_users=Count("userId"),
            active_users=Count(
                "userId", filter=Q(is_active=True, last_login__isnull=False)
            ),
            inactive_users=Count("userId", filter=Q(is_active=False)),
            pending_users=Count(
                "userId", filter=Q(is_active=True, last_login__isnull=True)
            ),
            # This count is now dynamic based on the date range
            new_users_in_period=Count(
                "userId", filter=Q(createdAt__range=[start_date_dt, end_date_dt])
            ),
        )

        # --- 3. Active User (Engagement) Metric from AuditLog ---
        active_users_in_period = (
            AuditLog.objects.filter(
                action="login", timestamp__range=[start_date_dt, end_date_dt]
            )
            .values("user_id")
            .distinct()
            .count()
        )

        # --- 4. Role and Trend Aggregations ---
        role_distribution = (
            User.objects.filter(role__isnull=False)
            .values("role__name", "role__color")
            .annotate(count=Count("userId"))
            .order_by("-count")
        )

        # Trend is now also based on the provided date range
        from django.db.models.functions import TruncDay

        registration_trend = (
            User.objects.filter(createdAt__range=[start_date_dt, end_date_dt])
            .annotate(day=TruncDay("createdAt"))
            .values("day")
            .annotate(count=Count("userId"))
            .order_by("day")
        )

        # --- 5. Assemble Response ---
        return Response(
            {
                "total_users": user_counts["total_users"],
                "active_users": user_counts["active_users"],  # This is "ever active"
                "inactive_users": user_counts["inactive_users"],
                "pending_users": user_counts["pending_users"],
                "new_users_in_period": user_counts[
                    "new_users_in_period"
                ],  # Dynamic name
                "active_users_in_period": active_users_in_period,  # True engagement metric
                "role_distribution": list(role_distribution),
                "registration_trend": [
                    {
                        "day": item["day"].strftime("%Y-%m-%d"),
                        "registrations": item.get("count", 0),
                    }
                    for item in registration_trend
                ],
                "query_start_date": start_date_dt.strftime("%Y-%m-%d"),
                "query_end_date": end_date_dt.strftime("%Y-%m-%d"),
            }
        )

    def _log_user_action(self, target_user, action, details, metadata=None):
        """Helper method to log user actions for audit trail"""
        # Ensure request user exists and is authenticated
        requesting_user = getattr(self.request, "user", None)
        if not requesting_user or not requesting_user.is_authenticated:
            logger.warning(
                f"Attempted to log audit action without authenticated user. Action: {action}, Target: {target_user.email if target_user else 'None'}"
            )
            return  # Don't log if no request user

        try:
            AuditLog.objects.create(
                user=requesting_user,
                user_email=requesting_user.email,
                action=action,
                details=details,
                target_user=target_user,
                target_model="User",
                target_id=str(target_user.userId) if target_user else None,
                ip_address=self.request.META.get("REMOTE_ADDR"),
                user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
                metadata=metadata or {},
            )
        except Exception as e:
            # Log error but don't crash the main request
            logger.error(
                f"Failed to create audit log: Action={action}, User={requesting_user.email}, Target={target_user.email if target_user else 'None'}, Error={str(e)}"
            )
