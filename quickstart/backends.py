from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model

UserModel = get_user_model()

class RolePermissionBackend(ModelBackend):
    """
    Authenticates against settings.AUTH_USER_MODEL and checks permissions
    assigned via the custom 'role' attribute.
    """

    def has_perm(self, user_obj, perm, obj=None):
        """
        Returns True if the user has the specified permission through their role.
        Also respects is_superuser and explicit user permissions via inheritance.
        """
        # First, let the default ModelBackend handle is_active, is_superuser,
        # and directly assigned user permissions. If it grants permission, we're done.
        if super().has_perm(user_obj, perm, obj=obj):
            return True

        # If the default backend didn't grant permission, and the user is inactive
        # or anonymous, they definitely don't have permission.
        if not user_obj.is_active or user_obj.is_anonymous:
            return False

        # Now, check permissions assigned via the user's custom Role
        # Check if user_obj has the 'role' attribute and it's not None
        if not hasattr(user_obj, 'role') or user_obj.role is None:
            return False

        # Check if the role has the required permission
        # Assumes 'perm' is in the format 'app_label.codename'
        try:
            app_label, codename = perm.split('.')
        except ValueError:
            # Invalid permission format
            return False

        # Check if the permission exists within the role's permissions set
        # This is the core check against your custom Role model
        if user_obj.role.permissions.filter(content_type__app_label=app_label, codename=codename).exists():
            return True

        # If permission not found in role, deny
        return False

    # We don't necessarily need to override authenticate() or get_user()
    # unless we change how users log in. We primarily care about has_perm here.
    # Inheriting from ModelBackend keeps the standard user lookup functional.

    def get_all_permissions(self, user_obj, obj=None):
        if not user_obj.is_active or user_obj.is_anonymous or not hasattr(user_obj, 'role') or user_obj.role is None:
            return set()
        perms = set()
        for p in user_obj.role.permissions.select_related('content_type'):
             perms.add(f"{p.content_type.app_label}.{p.codename}")
        return perms