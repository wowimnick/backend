from rest_framework import viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
import logging

from ..models import Role
from ..serializers import RoleSerializer
from ..utils.permissions import IsAdminUser, check_user_role

logger = logging.getLogger(__name__)

class UserRoleView(APIView):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            # Get current user role
            role = request.user.role.name if request.user.role else None
            
            # Return role with hierarchy information
            role_hierarchy = {
                'Super Admin': 100,
                'Admin': 90,
                'Business Owner': 80,
                'Manager': 70,
                'Instructor': 60,
                'Content Creator': 50,
                'Student': 10,
                'Guest': 0
            }
            
            return Response({
                'role': role,
                'roleLevel': role_hierarchy.get(str(role) if role else '', 0),
                'allowedRoles': [r for r, level in role_hierarchy.items() 
                               if level <= role_hierarchy.get(str(role) if role else '', 0)]
            })
            
        except Exception as e:
            return Response({
                'error': str(e),
                'role': None,
                'roleLevel': 0,
                'allowedRoles': []
            }, status=400)

class RoleViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        if check_user_role(self.request.user, ['Admin', 'Super Admin']):
            return Role.objects.all()
        return Role.objects.none()