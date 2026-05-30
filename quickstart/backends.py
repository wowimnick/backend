from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

# MODIFIED: Import the BusinessStaff model
from quickstart.models import BusinessStaff

UserModel = get_user_model()


class RolePermissionBackend(ModelBackend):
    """
    MODIFIED: Authenticates against the user model and checks permissions from BOTH
    the user's global role and their active business staff role.
    """

    def has_perm(self, user_obj, perm, obj=None):
        """
        Returns True if the user has the specified permission.
        This now checks in the following order:
        1. Superuser / explicit user permissions (via super()).
        2. Permissions from the user's global role (e.g., 'Student').
        3. Permissions from the user's active business role.
        """
        # 1. Default checks for superuser, is_active, and direct user perms
        if super().has_perm(user_obj, perm, obj=obj):
            return True

        if not user_obj.is_active or user_obj.is_anonymous:
            return False

        try:
            app_label, codename = perm.split(".")
        except ValueError:
            return False

        # 2. Check the user's global role
        if hasattr(user_obj, "role") and user_obj.role:
            if user_obj.role.permissions.filter(
                content_type__app_label=app_label, codename=codename
            ).exists():
                return True

        # 3. MODIFIED: Check the user's active business role
        try:
            staff_entry = BusinessStaff.objects.select_related("role").get(
                user=user_obj, status="accepted"
            )
            if (
                staff_entry.role
                and staff_entry.role.permissions.filter(
                    content_type__app_label=app_label, codename=codename
                ).exists()
            ):
                return True
        except BusinessStaff.DoesNotExist:
            # The user is not a staff member of any business, so we just fall through.
            pass

        # If no permission was found in any of the checks, deny.
        return False

    def get_all_permissions(self, user_obj, obj=None):
        """
        MODIFIED: Django user/group/superuser perms (via ModelBackend) plus global
        role and active business staff role permissions.

        ModelBackend.has_perm() ends up calling self.get_all_permissions() on this
        backend, so we must include super().get_all_permissions() or superusers
        and direct user_permissions / groups would be ignored.
        """
        if not user_obj.is_active or user_obj.is_anonymous:
            return set()

        perms = set(super().get_all_permissions(user_obj, obj=obj))

        if hasattr(user_obj, "role") and user_obj.role:
            for p in user_obj.role.permissions.select_related("content_type"):
                perms.add(f"{p.content_type.app_label}.{p.codename}")

        try:
            staff_entry = (
                BusinessStaff.objects.select_related("role")
                .prefetch_related("role__permissions__content_type")
                .get(user=user_obj, status="accepted")
            )
            if staff_entry.role:
                for p in staff_entry.role.permissions.all():
                    perms.add(f"{p.content_type.app_label}.{p.codename}")
        except BusinessStaff.DoesNotExist:
            pass

        return perms
