from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from django.db.models import Count, Q, Prefetch

# MODIFIED: Import Permission model
from django.contrib.auth.models import Permission


from quickstart.models import (
    BusinessRole,
    BusinessInfo,
    PermissionGroup,
    EnhancedPermission,
    BusinessStaff,  # ADDED
)
from quickstart.serializers.business.business_staff_serializers import (
    BusinessRoleSerializer,
    PermissionGroupSerializer,
)

# MODIFIED: Removed IsBusinessOwnerOrManager as we will perform a more specific check.
from quickstart.utils.permissions import IsBusinessOwnerOrManager


class BusinessRoleViewSet(viewsets.ModelViewSet):
    """
    API endpoint for business owners/managers to manage custom roles
    and their permissions within their own business.
    """

    serializer_class = BusinessRoleSerializer
    # MODIFIED: The base permission is just being authenticated.
    # Specific permission checks will be done inside the methods.
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """
        This view should return a list of all the roles for
        the currently authenticated user's business.
        """
        user = self.request.user
        business = (
            BusinessInfo.objects.filter(Q(owner=user) | Q(staff_members__user=user))
            .distinct()
            .first()
        )

        if not business:
            return BusinessRole.objects.none()

        # Annotate each role with the count of staff members assigned to it.
        return BusinessRole.objects.filter(business=business).annotate(
            user_count=Count("businessstaff")
        )

    # --- HELPER FOR PERMISSION CHECK ---
    def _check_role_management_permission(self):
        """
        Checks if the request user has permission to manage roles for their business.
        This is a centralized check to be used by all action methods.
        """
        user = self.request.user
        business = (
            BusinessInfo.objects.filter(Q(owner=user) | Q(staff_members__user=user))
            .distinct()
            .first()
        )

        if not business:
            raise PermissionDenied("You are not associated with any business.")

        # The business owner always has permission.
        if business.owner == user:
            return business  # Return business for use in the calling method

        # Check if the user is a staff member with the specific permission.
        staff_profile = BusinessStaff.objects.filter(
            user=user, business=business
        ).first()
        if staff_profile and staff_profile.role:
            if staff_profile.role.permissions.filter(
                codename="manage_business_roles"
            ).exists():
                return business  # Return business for use in the calling method

        raise PermissionDenied(
            "You do not have permission to manage roles and permissions."
        )

    def update(self, request, *args, **kwargs):
        """
        Handle role updates with an added security check to prevent staff
        from modifying the permissions of their own role.
        """
        user = request.user
        role_to_update = self.get_object()

        # The business owner is exempt from this check.
        if role_to_update.business.owner != user:
            staff_profile = BusinessStaff.objects.filter(
                user=user, business=role_to_update.business
            ).first()

            # If the user is a staff member and is editing their own role
            if staff_profile and staff_profile.role == role_to_update:
                # And if they are trying to change the 'permissions' field
                if "permissions" in request.data:
                    # Deny the request.
                    raise PermissionDenied(
                        "You cannot change the permissions of your own role."
                    )

        # If the security check passes, proceed with the standard update.
        return super().update(request, *args, **kwargs)

    def perform_create(self, serializer):
        """Associate the new role with the user's business and add base permissions."""
        # MODIFIED: Use the centralized permission check.
        business = self._check_role_management_permission()

        # Save the role first
        instance = serializer.save(business=business)

        # MODIFIED: Always add the base dashboard access permission
        try:
            dashboard_perm = Permission.objects.get(
                codename="access_business_dashboard"
            )
            instance.permissions.add(dashboard_perm)
        except Permission.DoesNotExist:
            # Handle case where permission might not exist (unlikely in a migrated system)
            # You could log a warning here
            pass

    def perform_update(self, serializer):
        """Ensure the base dashboard access permission cannot be removed."""
        # MODIFIED: Use the centralized permission check.
        self._check_role_management_permission()
        instance = serializer.save()
        try:
            dashboard_perm = Permission.objects.get(
                codename="access_business_dashboard"
            )
            # The .add() method is idempotent, so it's safe to call even if the permission is already there.
            instance.permissions.add(dashboard_perm)
        except Permission.DoesNotExist:
            pass  # Log a warning if necessary

    def perform_destroy(self, instance):
        """
        Prevents deletion of a role if it is currently assigned to any staff members.
        The model's `on_delete=PROTECT` provides the database-level protection.
        """
        # MODIFIED: Use the centralized permission check.
        self._check_role_management_permission()
        if instance.businessstaff_set.exists():
            raise PermissionDenied(
                "Cannot delete a role that is currently assigned to staff members."
            )
        super().perform_destroy(instance)

    @action(detail=False, methods=["get"], url_path="available-permissions")
    def available_permissions(self, request):
        """
        Provides a structured list of permissions that can be assigned to
        business roles, grouped by UI category for the business role editor.
        """
        # This is a read-only action, but we can still lock it down.
        self._check_role_management_permission()

        limit_choices = BusinessRole._meta.get_field(
            "permissions"
        ).get_limit_choices_to()
        allowed_codenames = limit_choices.get("codename__in", [])

        # --- UPDATED QUERY ---
        # Filter groups to only those with the 'business_role_editor' UI category.
        queryset = (
            PermissionGroup.objects.filter(ui_category="business_role_editor")
            .prefetch_related(
                Prefetch(
                    "permissions",
                    queryset=EnhancedPermission.objects.filter(
                        permission__codename__in=allowed_codenames
                    ).select_related("permission"),
                )
            )
            .order_by("sort_order", "name")
        )

        response_data = []
        for group in queryset:
            permissions_data = []
            for ep in group.permissions.all():
                # Format the response to use the clear description as the main 'name'
                permissions_data.append(
                    {
                        "id": ep.permission.id,
                        "name": ep.description or ep.permission.name,
                        "description": "",  # Keep this for frontend component compatibility
                    }
                )

            # Only add the group (tab) if it actually has permissions
            if permissions_data:
                response_data.append(
                    {
                        "id": group.id,
                        "name": group.name,  # This is the tab name, e.g., "Profile & Staff"
                        "permissions": permissions_data,
                    }
                )

        return Response(response_data)
