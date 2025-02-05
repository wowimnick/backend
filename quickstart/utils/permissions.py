from rest_framework import permissions
import logging

from quickstart.models import BusinessInfo

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
    # First check if user is authenticated and has a role
    if not user or not hasattr(user, 'role') or not user.role:
        return False
        
    logger.debug(f"User: {user.role.name}")
    return user.role.name in allowed_roles

def check_user_can_create_class(user, business_id):
    """Returns bool if user can create classes for this business"""
    if user.has_role(['Admin', 'Super Admin']):
        return True
    
    business = BusinessInfo.objects.get(pk=business_id)
    return (
        business.owner == user or 
        user in business.managers.all() or
        user.has_role('Content Creator')
    )

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
        # Allow GET requests without authentication
        if request.method == 'GET':
            return True
            
        # For other methods, require authentication and proper role
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