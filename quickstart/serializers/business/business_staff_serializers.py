import os
from django.conf import settings
from rest_framework import serializers
from django.contrib.auth.models import Permission
from quickstart.models import (
    BusinessStaff,
    Role,
    BusinessRole,
    PermissionGroup,
    EnhancedPermission,
)


class BusinessStaffSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.get_full_name", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)

    class Meta:
        model = BusinessStaff
        fields = [
            "id",
            "user_name",
            "user_email",
            "invited_email",
            "role",
            "role_name",
            "status",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "user_name",
            "user_email",
            "invited_email",
            "status",
            "created_at",
            "role_name",
        ]


class StaffInviteSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessStaff
        fields = ["invited_email", "role"]


# --- NEW/UPDATED SERIALIZERS ---


class PermissionSerializer(serializers.ModelSerializer):
    """Basic serializer for permission data."""

    description = serializers.CharField(source="enhanced.description", read_only=True)

    class Meta:
        model = Permission
        fields = ["id", "name", "description"]


class PermissionGroupSerializer(serializers.ModelSerializer):
    """Serializer to group permissions for the UI."""

    permissions = PermissionSerializer(many=True, read_only=True)

    class Meta:
        model = PermissionGroup
        fields = ("id", "name", "permissions")


class BusinessRoleSerializer(serializers.ModelSerializer):
    """Serializer for creating and managing business-specific roles."""

    user_count = serializers.IntegerField(read_only=True)
    permissions = serializers.PrimaryKeyRelatedField(
        queryset=Permission.objects.all(), many=True, required=False
    )

    class Meta:
        model = BusinessRole
        fields = ["id", "name", "description", "permissions", "user_count"]
        read_only_fields = ["id", "user_count"]

    def validate_permissions(self, value):
        """
        Ensures that only permissions designated as assignable to businesses
        can be added to a business role.
        """
        limit_choices = BusinessRole._meta.get_field(
            "permissions"
        ).get_limit_choices_to()
        allowed_codenames = set(limit_choices.get("codename__in", []))

        for perm in value:
            if perm.codename not in allowed_codenames:
                raise serializers.ValidationError(
                    f"Permission '{perm.name}' is not assignable to business roles."
                )
        return value


class InvitationDetailsSerializer(serializers.ModelSerializer):
    """
    Serializer for displaying public details of a pending staff invitation.
    """

    inviter_name = serializers.CharField(
        source="invited_by.get_full_name", read_only=True
    )
    business_name = serializers.CharField(
        source="business.businessName", read_only=True
    )
    role_name = serializers.CharField(source="role.name", read_only=True)

    business_image_medium_url = serializers.SerializerMethodField()

    class Meta:
        model = BusinessStaff
        fields = [
            "invited_email",
            "inviter_name",
            "business_name",
            "role_name",
            "business_image_medium_url",
        ]

    def get_business_image_medium_url(self, obj):
        if not obj.business.businessImage or not obj.business.businessImage.name:
            return None
        original_path = obj.business.businessImage.name
        if not original_path.startswith("originals/"):
            return None
        # Split the path to separate filename and extension
        base_path = os.path.splitext(original_path)[0]
        # Replace directory and append .webp extension
        resized_path = base_path.replace("originals/", "public/medium/", 1) + ".webp"
        return f"{settings.CLOUDFRONT_DOMAIN}/{resized_path}"
