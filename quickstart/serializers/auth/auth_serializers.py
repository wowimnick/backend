# quickstart/serializers/auth_serializers.py

import os
from django.conf import settings
from rest_framework import serializers
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
from quickstart.models import Role, ClassesMain, BusinessInfo
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

    avatar = serializers.ImageField(write_only=True, required=False, allow_null=True)
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
            "birth_date",
            "bio",
            "phone_number",
            "country",
            "city",
            "state",
            "address",
            "zipCode",
            "avatar",
            "avatar_thumb_url",
            "avatar_medium_url",
            "avatar_original_url",
            "role",
            "user_timezone",
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
            return None

        original_path = obj.avatar.name
        if not original_path.startswith("originals/"):
            return None

        if size:
            final_path = original_path.replace("originals/", f"public/{size}/", 1)
        else:
            final_path = original_path

        return f"{settings.CLOUDFRONT_DOMAIN}/{final_path}"

    def get_avatar_thumb_url(self, obj):
        return self._get_avatar_url(obj, "thumb")

    def get_avatar_medium_url(self, obj):
        return self._get_avatar_url(obj, "medium")

    def get_avatar_original_url(self, obj):
        return self._get_avatar_url(obj, None)

    def get_permissions(self, user):
        if not user or not user.is_authenticated:
            return []
        return list(user.get_all_permissions())

    def get_has_business(self, user):
        if not user or not user.is_authenticated:
            return False
        return BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).exists()

    def update(self, instance, validated_data):
        avatar_file = validated_data.pop("avatar", "NOT_PROVIDED")
        instance = super().update(instance, validated_data)
        if avatar_file is None:
            if instance.avatar:
                instance.avatar.delete(save=False)
                instance.avatar = None
        elif isinstance(avatar_file, InMemoryUploadedFile):
            if instance.avatar:
                instance.avatar.delete(save=False)
            instance.avatar = avatar_file
        instance.save()
        return instance


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
    birth_date = serializers.DateField(required=True)
    phone_number = serializers.CharField(required=True, max_length=20)
    bio = serializers.CharField(required=False, allow_blank=True)
    avatar = serializers.ImageField(required=False, allow_null=True)
    user_timezone = serializers.CharField(required=True, max_length=50)

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
                "birth_date": self.validated_data.get("birth_date", None),
                "phone_number": self.validated_data.get("phone_number", ""),
                "bio": self.validated_data.get("bio", ""),
                "avatar": self.validated_data.get("avatar", None),
                "user_timezone": self.validated_data.get(
                    "user_timezone", "America/Toronto"
                ),
            }
        )
        return data

    def save(self, request):
        user = super().save(request)
        user.first_name = self.validated_data.get("first_name", "")
        user.last_name = self.validated_data.get("last_name", "")
        user.birth_date = self.validated_data.get("birth_date", None)
        user.phone_number = self.validated_data.get("phone_number", "")
        user.bio = self.validated_data.get("bio", "")
        user.avatar = self.validated_data.get("avatar", None)
        user.user_timezone = self.validated_data.get("user_timezone", "America/Toronto")

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
