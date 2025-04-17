from rest_framework.permissions import BasePermission
from django.db.models import Q
import logging

from quickstart.models import BusinessInfo

logger = logging.getLogger(__name__)

class CanAccessBusinessDashboard(BasePermission):
    """Allows access if user has 'access_business_dashboard' permission AND owns/manages the business."""
    message = "You do not have permission to access the business dashboard."

    def has_permission(self, request, view):
        # Base check: must be authenticated and have the specific dashboard permission.
        return (
            request.user and
            request.user.is_authenticated and
            request.user.has_perm('quickstart.access_business_dashboard')
        )

    def has_object_permission(self, request, view, obj):
        """ Check if user owns/manages the specific business object. """
        # obj is the BusinessInfo instance
        user = request.user
        # Ensure the user also has the base permission (redundant but safe)
        if not self.has_permission(request, view):
            return False

        # Check ownership/management relationship
        is_owner = obj.owner == user
        is_manager = obj.managers.filter(pk=user.pk).exists()
        can_access = is_owner or is_manager

        if not can_access:
            logger.warning(f"Dashboard access denied for user {user.email} on business {obj.businessId}. Doesn't own/manage.")
        return can_access

class CanManageOwnBusinessProfile(BasePermission):
    """
    Allows access only if the user owns or manages the specific BusinessInfo object.
    Used for viewing/editing their own profile.
    """
    message = "You do not have permission to manage this business profile."

    def has_permission(self, request, view):
        # Check if the user is authenticated (already done by IsAuthenticated)
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        # Check if the request user is the owner or one of the managers
        # Ensure obj is a BusinessInfo instance
        if not hasattr(obj, 'owner') or not hasattr(obj, 'managers'):
            return False # Should not happen if used correctly

        user = request.user
        # Check ownership OR if user is in the managers ManyToMany relationship
        return obj.owner == user or obj.managers.filter(pk=user.pk).exists()

class CanDeleteOwnBusinessProfile(BasePermission):
    """
    Allows access only if the user is the OWNER of the BusinessInfo object.
    Used specifically for deleting a business profile.
    """
    message = "Only the business owner can delete the profile."

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        if not hasattr(obj, 'owner'):
             return False
        # Only the owner can delete
        return obj.owner == request.user

class CanManageOwnClasses(BasePermission):
    """
    Allows access if user has 'manage_own_classes' permission AND
    the class/option/schedule/instance object belongs to their associated business.
    """
    message = "You do not have permission to manage classes/schedules for this business."

    def has_permission(self, request, view):
        """
        Check if the user is authenticated and has the base permission
        required to manage any class content for their own business.
        """
        user = request.user
        return (
            user and
            user.is_authenticated and
            user.has_perm('quickstart.manage_own_classes') # Check the specific permission codename
        )

    def has_object_permission(self, request, view, obj):
        """
        Check if the user owns or manages the business associated with the object
        (Class, Option, Schedule, Instance, etc.).
        """
        user = request.user

        # Double-check base permission (though usually redundant if used in view's permission_classes)
        if not self.has_permission(request, view):
            return False

        # Find the business associated with the requesting user
        # Use filter().first() to handle cases where user might somehow be linked to multiple (though unlikely for direct ownership/management)
        user_business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()

        if not user_business:
            logger.warning(f"Permission check failed for user {user.email}: User has 'manage_own_classes' perm but no associated business.")
            return False # User has the perm but isn't linked to any business

        # Determine the business associated with the object being accessed (obj)
        object_business = None
        try:
            # Navigate up the relationship chain to find the BusinessInfo instance
            if hasattr(obj, 'businessId'): # If obj is ClassesMain
                object_business = obj.businessId
            elif hasattr(obj, 'classId') and hasattr(obj.classId, 'businessId'): # If obj is ClassOption or ClassImage
                object_business = obj.classId.businessId
            elif hasattr(obj, 'schedule') and hasattr(obj.schedule, 'option'): # If obj is ScheduleInstance or ScheduleBreak
                 object_business = obj.schedule.option.classId.businessId
            elif hasattr(obj, 'option') and hasattr(obj.option, 'classId'): # If obj is Schedule
                 object_business = obj.option.classId.businessId
            # Add more checks if other related objects need this permission

            if not object_business:
                 logger.error(f"Could not determine business association for object type {type(obj)} with pk {getattr(obj, 'pk', 'N/A')} during permission check.")
                 return False # Cannot determine ownership

            # Check if the object's business matches the user's associated business
            is_authorized = (object_business == user_business)

            if not is_authorized:
                 logger.warning(f"Permission denied for user {user.email} on object {obj} (Business: {object_business.businessId}). User manages Business: {user_business.businessId}.")

            return is_authorized

        except AttributeError as e:
             logger.error(f"AttributeError during object permission check for user {user.email} on object {obj}: {e}", exc_info=True)
             return False # Error resolving relationship chain
         
