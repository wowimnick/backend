from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth.models import Permission
from django.db.models import Count, Prefetch, Max
from django.contrib.contenttypes.models import ContentType
import logging

from quickstart.serializers.admin.user_management.role_serializers import (
    RoleCreateSerializer,
    PermissionGroupSerializer,
    RoleDetailSerializer,
)

from quickstart.models import Role, PermissionGroup, EnhancedPermission, AuditLog
from quickstart.utils.permissions import CanAccessUserAdmin

logger = logging.getLogger(__name__)


class RoleManagementViewSet(viewsets.ModelViewSet):
    """Viewset for role management"""

    permission_classes = [IsAuthenticated, CanAccessUserAdmin]

    def get_queryset(self):
        queryset = Role.objects.annotate(user_count=Count("customuser"))

        # Implement filtering if needed
        search = self.request.query_params.get("search")
        if search:
            queryset = queryset.filter(name__icontains=search)

        return queryset.order_by("name")

    def get_serializer_class(self):
        if self.action == "create":
            return RoleCreateSerializer
        return RoleDetailSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        # Don't allow deletion of system roles
        if instance.is_system:
            return Response(
                {"detail": "System roles cannot be deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if users are assigned to this role
        if instance.user_count > 0:
            return Response(
                {
                    "detail": f"Cannot delete role with {instance.user_count} users assigned."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Determine if the requester has super admin privileges
        is_super_admin_request = request.user.is_superuser or (
            request.user.role and request.user.role.name == "Super Admin"
        )

        # Non-super admins are subject to hierarchy checks
        if not is_super_admin_request:
            user_role = request.user.role
            # Check if user has sufficient privileges to delete this role
            if user_role and user_role.hierarchy_level <= instance.hierarchy_level:
                return Response(
                    {
                        "detail": "You cannot delete a role with equal or higher privileges than your own."
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Log the action
        try:
            AuditLog.objects.create(
                user=self.request.user,
                user_email=self.request.user.email,
                action="role_delete",
                details=f"Role '{instance.name}' deleted",
                target_model="Role",
                target_id=str(instance.id),
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception as e:
            logger.error(f"Failed to create audit log: {str(e)}")

        return super().destroy(request, *args, **kwargs)

    # Optimize by fetching content types in a single query
    def _get_excluded_content_types(self):
        """Get content types to exclude in a single query"""
        excluded_apps = [
            "admin",
            "admin_interface",
            "allauth",
            "account",
            "auth",
            "authtoken",
            "contenttypes",
            "sessions",
            "sites",
            "socialaccount",
            "silk",
            "theme",
            "token_blacklist",
        ]

        return ContentType.objects.filter(app_label__in=excluded_apps)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Determine if the requester has super admin privileges
        is_super_admin_request = request.user.is_superuser or (
            request.user.role and request.user.role.name == "Super Admin"
        )

        # Non-super admins are subject to hierarchy checks
        if not is_super_admin_request:
            user_role = request.user.role
            new_role_level = serializer.validated_data.get("hierarchy_level", 0)
            if not user_role or user_role.hierarchy_level <= new_role_level:
                return Response(
                    {
                        "detail": "You cannot create a role with privileges equal to or greater than your own."
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Filter out Django internal permissions - optimized
        if "permissions" in request.data:
            excluded_content_types = self._get_excluded_content_types()
            permissions = serializer.validated_data.get("permissions", [])
            filtered_permissions = [
                p
                for p in permissions
                if p.content_type_id
                not in excluded_content_types.values_list("id", flat=True)
            ]
            serializer.validated_data["permissions"] = filtered_permissions

        instance = serializer.save()

        # Log the action
        try:
            AuditLog.objects.create(
                user=self.request.user,
                user_email=self.request.user.email,
                action="role_create",
                details=f"Role '{instance.name}' created",
                target_model="Role",
                target_id=str(instance.id),
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception as e:
            logger.error(f"Failed to create audit log: {str(e)}")

        headers = self.get_success_headers(serializer.data)
        return Response(
            RoleDetailSerializer(instance).data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()

        # Determine if the requester has super admin privileges
        is_super_admin_request = request.user.is_superuser or (
            request.user.role and request.user.role.name == "Super Admin"
        )

        # Non-super admins are subject to hierarchy and permission checks
        if not is_super_admin_request:
            user_role = request.user.role
            is_editing_own_role = user_role == instance

            if is_editing_own_role:
                # User can edit their own role only if they have the highest level
                max_level = Role.objects.aggregate(max_level=Max("hierarchy_level"))[
                    "max_level"
                ]
                if user_role.hierarchy_level < max_level:
                    return Response(
                        {
                            "detail": "You cannot edit your own role unless you have the highest hierarchy level."
                        },
                        status=status.HTTP_403_FORBIDDEN,
                    )
            else:
                # For other roles, user must have a strictly higher hierarchy level
                if (
                    not user_role
                    or user_role.hierarchy_level <= instance.hierarchy_level
                ):
                    return Response(
                        {
                            "detail": "You cannot edit a role with equal or higher privileges than your own."
                        },
                        status=status.HTTP_403_FORBIDDEN,
                    )

            if instance.name == "SuperAdmin" and user_role.name != "SuperAdmin":
                return Response(
                    {"detail": "Only SuperAdmin users can edit the SuperAdmin role."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Get current permissions
        current_permission_ids = set(instance.permissions.values_list("id", flat=True))

        # Handle permissions if they're provided
        if "permissions" in request.data and request.data["permissions"]:
            # Get new permissions
            new_permission_ids = set(request.data["permissions"])

            # Calculate permissions to add and remove
            to_add = new_permission_ids - current_permission_ids
            to_remove = current_permission_ids - new_permission_ids

            modified_data = request.data.copy()
            if "permissions" in modified_data:
                del modified_data["permissions"]

            serializer = self.get_serializer(instance, data=modified_data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_instance = serializer.save()

            if to_add:
                updated_instance.permissions.add(*to_add)
            if to_remove:
                updated_instance.permissions.remove(*to_remove)

            if to_add or to_remove:
                try:
                    AuditLog.objects.create(
                        user=self.request.user,
                        user_email=self.request.user.email,
                        action="role_update",
                        details=f"Role '{instance.name}' updated with permission changes",
                        target_model="Role",
                        target_id=str(instance.id),
                        ip_address=request.META.get("REMOTE_ADDR"),
                        user_agent=request.META.get("HTTP_USER_AGENT", ""),
                        metadata={
                            "permission_changes": {
                                "added": list(to_add),
                                "removed": list(to_remove),
                            }
                        },
                    )
                except Exception as e:
                    logger.error(f"Failed to create audit log: {str(e)}")
        else:
            serializer = self.get_serializer(
                instance, data=request.data, partial=partial
            )
            serializer.is_valid(raise_exception=True)
            updated_instance = serializer.save()

            try:
                AuditLog.objects.create(
                    user=self.request.user,
                    user_email=self.request.user.email,
                    action="role_update",
                    details=f"Role '{instance.name}' updated",
                    target_model="Role",
                    target_id=str(instance.id),
                    ip_address=request.META.get("REMOTE_ADDR"),
                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                )
            except Exception as e:
                logger.error(f"Failed to create audit log: {str(e)}")

        return Response(RoleDetailSerializer(updated_instance).data)

    @action(detail=False, methods=["post"])
    def update_order(self, request):
        """Update the hierarchy levels of multiple roles at once"""
        roles_data = request.data

        if not isinstance(roles_data, list):
            return Response(
                {"detail": "Invalid data format. Expected a list of role objects."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Verify that all required fields are present
        for item in roles_data:
            if "id" not in item or "hierarchy_level" not in item:
                return Response(
                    {"detail": "Each item must contain id and hierarchy_level fields."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            with transaction.atomic():
                updated_roles = []
                user_role = request.user.role

                # Determine if the requester has super admin privileges
                is_super_admin_request = request.user.is_superuser or (
                    request.user.role and request.user.role.name == "Super Admin"
                )

                for item in roles_data:
                    role = Role.objects.get(id=item["id"])

                    # Non-super admins are subject to hierarchy checks
                    if not is_super_admin_request:
                        # Prevent user from editing roles at or above their own level
                        if (
                            user_role
                            and user_role.hierarchy_level <= role.hierarchy_level
                        ):
                            return Response(
                                {
                                    "detail": f"You cannot modify role '{role.name}' with equal or higher privileges than your own."
                                },
                                status=status.HTTP_403_FORBIDDEN,
                            )

                    role.hierarchy_level = item["hierarchy_level"]
                    role.save(update_fields=["hierarchy_level"])
                    updated_roles.append(role)

                # Log the action
                try:
                    AuditLog.objects.create(
                        user=self.request.user,
                        user_email=self.request.user.email,
                        action="role_update",
                        details=f"Role hierarchy order updated for {len(updated_roles)} roles",
                        target_model="Role",
                        ip_address=request.META.get("REMOTE_ADDR"),
                        user_agent=request.META.get("HTTP_USER_AGENT", ""),
                    )
                except Exception as e:
                    logger.error(f"Failed to create audit log: {str(e)}")

                serializer = RoleDetailSerializer(updated_roles, many=True)
                return Response(serializer.data)
        except Role.DoesNotExist:
            return Response(
                {"detail": "One or more roles not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception as e:
            logger.error(f"Error updating role hierarchy: {str(e)}")
            return Response(
                {"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """Duplicate an existing role"""
        source_role = self.get_object()
        new_name = request.data.get("name", f"{source_role.name} (Copy)")

        if Role.objects.filter(name=new_name).exists():
            return Response(
                {"detail": "A role with this name already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_role = Role.objects.create(
            name=new_name,
            description=source_role.description,
            is_system=False,
            is_default=False,
            color=source_role.color,
            hierarchy_level=max(1, source_role.hierarchy_level - 1),
        )

        # Copy permissions - filter out excluded ones
        excluded_apps = [
            "admin",
            "auth",
            "contenttypes",
            "sessions",
            "sites",
            "theme",
            "silk",
            "allauth",
            "account",
            "socialaccount",
        ]
        excluded_content_types = ContentType.objects.filter(app_label__in=excluded_apps)
        permissions_to_copy = source_role.permissions.exclude(
            content_type__in=excluded_content_types
        )
        new_role.permissions.set(permissions_to_copy)

        # Log the action
        try:
            AuditLog.objects.create(
                user=self.request.user,
                user_email=self.request.user.email,
                action="role_create",
                details=f"Role '{new_role.name}' created as a copy of '{source_role.name}'",
                target_model="Role",
                target_id=str(new_role.id),
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception as e:
            logger.error(f"Failed to create audit log: {str(e)}")

        return Response(
            RoleDetailSerializer(new_role, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"])
    def permissions(self, request):
        """Get all available permissions grouped for the UI"""
        excluded_apps = [
            "admin",
            "auth",
            "contenttypes",
            "sessions",
            "sites",
            "theme",
            "silk",
            "allauth",
            "account",
            "socialaccount",
        ]

        excluded_content_types = ContentType.objects.filter(app_label__in=excluded_apps)
        excluded_content_type_ids = excluded_content_types.values_list("id", flat=True)

        enhanced_permissions = EnhancedPermission.objects.select_related(
            "permission", "permission__content_type", "group"
        ).exclude(permission__content_type_id__in=excluded_content_type_ids)

        permission_groups = PermissionGroup.objects.prefetch_related(
            Prefetch("permissions", queryset=enhanced_permissions)
        ).filter(
            id__in=enhanced_permissions.values_list("group_id", flat=True).distinct()
        )

        serializer = PermissionGroupSerializer(permission_groups, many=True)
        return Response(serializer.data)
