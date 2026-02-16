# quickstart/serializers/auth_serializers.py

import os
from django.conf import settings
from rest_framework import serializers

from quickstart.utils.url_utils import build_cloudfront_url
from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.serializers import LoginSerializer as DefaultLoginSerializer
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.db.models import Exists, OuterRef, Q
from allauth.account.adapter import get_adapter
from django.core.files.uploadedfile import InMemoryUploadedFile
from allauth.account.utils import (
    filter_users_by_email,
    get_next_redirect_url,
    passthrough_next_redirect_url,
)
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.sites.shortcuts import get_current_site
from quickstart.models import BusinessStaff, Role, ClassesMain, BusinessInfo
from django.contrib.auth.tokens import default_token_generator
import logging
from allauth.account.forms import ResetPasswordForm

logger = logging.getLogger(__name__)

User = get_user_model()


class CustomLoginSerializer(DefaultLoginSerializer):
    def get_fields(self):
        fields = super().get_fields()
        return fields

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return attrs


class RoleNestedSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ("id", "name", "color", "hierarchy_level")
        read_only_fields = fields


class CustomUserDetailsSerializer(serializers.ModelSerializer):
    avatar_thumb_url = serializers.SerializerMethodField()
    avatar_medium_url = serializers.SerializerMethodField()
    avatar_original_url = serializers.SerializerMethodField()

    avatar = serializers.CharField(write_only=True, required=False, allow_null=True)
    role = RoleNestedSerializer(read_only=True, allow_null=True)
    favorited_ids = serializers.PrimaryKeyRelatedField(
        source="favorited", many=True, read_only=True
    )
    permissions = serializers.SerializerMethodField(read_only=True)
    has_business = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = User
        fields = (
            "userId",
            "email",
            "username",
            "first_name",
            "last_name",
            "bio",
            "phone_number",
            "country",
            "city",
            "state",
            "address",
            "zipCode",
            "user_timezone",
            "avatar",
            "avatar_thumb_url",
            "avatar_medium_url",
            "avatar_original_url",
            "role",
            "favorited_ids",
            "permissions",
            "has_business",
        )
        read_only_fields = (
            "userId",
            "email",
            "username",
            "role",
            "avatar_thumb_url",
            "avatar_medium_url",
            "avatar_original_url",
            "favorited_ids",
            "permissions",
            "has_business",
        )
        extra_kwargs = {"email": {"read_only": True}, "username": {"read_only": True}}

    def _get_avatar_url(self, obj, size=None):
        if not obj.avatar or not hasattr(obj.avatar, "name") or not obj.avatar.name:
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            logger.warning("CLOUDFRONT_DOMAIN is not configured.")
            return obj.avatar.url

        original_path = obj.avatar.name
        if "originals/" not in original_path:
            return build_cloudfront_url(original_path)

        if size:
            base_name, _ = os.path.splitext(original_path.replace("originals/", "", 1))
            final_path = f"public/{size}/{base_name}.webp"
        else:
            final_path = original_path

        return build_cloudfront_url(final_path)

    def get_avatar_thumb_url(self, obj):
        return self._get_avatar_url(obj, "thumb")

    def get_avatar_medium_url(self, obj):
        return self._get_avatar_url(obj, "medium")

    def get_avatar_original_url(self, obj):
        if not obj.avatar or not hasattr(obj.avatar, "name") or not obj.avatar.name:
            return None
        return build_cloudfront_url(obj.avatar.name)

    def get_permissions(self, user):
        # MODIFIED: Combine base permissions with business role permissions
        if not user or not user.is_authenticated:
            return []

        # Start with the user's base permissions (from their global role)
        base_permissions = set(user.get_all_permissions())

        # Now, check for a business staff role (prefetch content_type to avoid N+1 on permissions)
        business_staff_entry = (
            BusinessStaff.objects.filter(user=user, status="accepted")
            .select_related("role")
            .prefetch_related("role__permissions__content_type")
            .first()
        )

        if business_staff_entry and business_staff_entry.role:
            business_permissions = {
                f"{perm.content_type.app_label}.{perm.codename}"
                for perm in business_staff_entry.role.permissions.all()
            }
            # Combine the two sets for the final list
            base_permissions.update(business_permissions)

        return list(base_permissions)

    def get_has_business(self, user):
        if not user or not user.is_authenticated:
            return False
        # A user has a business if they own one OR are an accepted staff member
        return (
            BusinessInfo.objects.filter(owner=user).exists()
            or BusinessStaff.objects.filter(user=user, status="accepted").exists()
        )

    def update(self, instance, validated_data):
        avatar_s3_key = validated_data.pop("avatar", "NOT_PROVIDED")
        instance = super().update(instance, validated_data)
        if avatar_s3_key is None:
            if instance.avatar:
                instance.avatar.delete(save=False)
            instance.avatar = None
        elif avatar_s3_key != "NOT_PROVIDED":
            instance.avatar = avatar_s3_key
        instance.save()
        return instance

    def to_representation(self, instance):
        # MODIFIED: Override to show the business role if it exists
        representation = super().to_representation(instance)

        # Find the user's active business staff role
        business_staff_entry = (
            BusinessStaff.objects.filter(user=instance, status="accepted")
            .select_related("role")
            .first()
        )

        if business_staff_entry and business_staff_entry.role:
            # If they have a business role, serialize it and replace the global role in the output
            business_role = business_staff_entry.role
            representation["role"] = {
                "id": business_role.id,  # Note: this is the BusinessRole ID
                "name": business_role.name,
                "color": "#64748b",  # Business roles don't have colors, provide a default or add to model
                "hierarchy_level": 100,  # Business roles have higher precedence
            }

        return representation


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    user = CustomUserDetailsSerializer(read_only=True)

    @classmethod
    def get_token(cls, user):
        return super().get_token(user)

    def validate(self, attrs):
        data = super().validate(attrs)
        user_serializer = CustomUserDetailsSerializer(self.user, context=self.context)
        data["user"] = user_serializer.data
        return data


class CustomRegisterSerializer(RegisterSerializer):
    first_name = serializers.CharField(required=True, max_length=150)
    last_name = serializers.CharField(required=True, max_length=150)
    phone_number = serializers.CharField(required=True, max_length=20)
    bio = serializers.CharField(required=False, allow_blank=True)
    avatar = serializers.ImageField(required=False, allow_null=True)
    # ADDED: Explicitly define the user_timezone field to accept it from the frontend
    user_timezone = serializers.CharField(
        required=False, max_length=50, allow_blank=True
    )

    def validate(self, data):
        email = data.get("email", "").lower()
        if email and User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                {
                    "email": serializers.ErrorDetail(
                        "A user is already registered with this e-mail address.",
                        code="unique",
                    )
                }
            )
        return data

    def get_cleaned_data(self):
        data = super().get_cleaned_data()
        data.update(
            {
                "first_name": self.validated_data.get("first_name", ""),
                "last_name": self.validated_data.get("last_name", ""),
                "phone_number": self.validated_data.get("phone_number", ""),
                "bio": self.validated_data.get("bio", ""),
                "avatar": self.validated_data.get("avatar", None),
                # ADDED: Pass the timezone through the cleaned data
                "user_timezone": self.validated_data.get("user_timezone", "UTC"),
            }
        )
        return data

    def save(self, request):
        user = super().save(request)
        user.first_name = self.validated_data.get("first_name", "")
        user.last_name = self.validated_data.get("last_name", "")
        user.phone_number = self.validated_data.get("phone_number", "")
        user.bio = self.validated_data.get("bio", "")
        user.avatar = self.validated_data.get("avatar", None)
        # ADDED: Set the timezone on the user model before saving
        user.user_timezone = self.validated_data.get("user_timezone", "UTC")

        if not user.role:
            try:
                default_role, created = Role.objects.get_or_create(
                    is_default=True,
                    defaults={"name": "Student", "hierarchy_level": 10},
                )
                user.role = default_role
            except Exception as e:
                logger.error(
                    f"Could not assign default role during registration for user {user.email}: {e}"
                )

        user.save()
        return user


class CustomAllAuthPasswordResetForm(ResetPasswordForm):
    def get_users(self, email):
        return filter_users_by_email(email, is_active=True)

    def save(self, request, **kwargs):
        current_site = get_current_site(request)
        email = self.cleaned_data["email"]
        token_generator = kwargs.get("token_generator", default_token_generator)
        users = self.get_users(email)

        for user in users:
            temp_key = token_generator.make_token(user)
            adapter = get_adapter(request)
            password_reset_url = adapter.get_password_reset_url(request, user, temp_key)
            logger.info(
                f"Correctly generated password reset URL via adapter: {password_reset_url}"
            )
            context = {
                "current_site": current_site,
                "user": user,
                "password_reset_url": password_reset_url,
                "request": request,
            }
            adapter.send_mail("account/email/password_reset_key", email, context)

        return self.cleaned_data["email"]
