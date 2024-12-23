from rest_framework import permissions
import logging

logger = logging.getLogger(__name__)

def check_user_role(user, allowed_roles):
    """
    Check if user has any of the allowed roles
    
    Args:
        user: User object
        allowed_roles: List of role names
    Returns:
        bool: True if user has any of the allowed roles
    """
    logger.debug(f"User: {user.role.name}")
    return user.role and user.role.name in allowed_roles

class BaseUserDataPermission(permissions.BasePermission):
    """
    Base permission class for user-related data access
    """
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        # Admin users can access all records
        if check_user_role(request.user, ['Admin', 'Super Admin']):
            return True
            
        # Check if the object belongs to the requesting user
        if hasattr(obj, 'user'):
            return obj.user == request.user
        if hasattr(obj, 'userId'):
            return obj.userId == request.user
        return False

class IsAdminUser(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and check_user_role(request.user, ['Admin', 'Super Admin'])

class IsBusinessOwner(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and check_user_role(
            request.user, 
            ['Business Owner', 'Admin', 'Super Admin']
        )

class IsManager(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and check_user_role(
            request.user,
            ['Manager', 'Business Owner', 'Admin', 'Super Admin']
        )

class IsInstructor(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and check_user_role(
            request.user,
            ['Instructor', 'Content Creator', 'Manager', 'Business Owner', 'Admin', 'Super Admin']
        )