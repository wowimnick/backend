from rest_framework import serializers
from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.serializers import LoginSerializer as DefaultLoginSerializer
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.db.models import Exists, OuterRef, Q
from django.core.files.uploadedfile import InMemoryUploadedFile
from ...models import Role, ClassesMain, BusinessInfo

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
    avatar_url = serializers.SerializerMethodField()
    # Allow avatar to be null on input to signal removal
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
            "avatar",  # Keep this write_only field
            "avatar_url",  # Read-only derived field
            "role",
            "user_timezone",  # ADDED user_timezone
            "favorited_ids",
            "permissions",
            "has_business",
        )
        read_only_fields = (
            "userId",
            "email",
            "username",
            "role",
            "avatar_url",
            "favorited_ids",
            "permissions",
            "has_business",
        )
        # Prevent accidental updates to sensitive fields during PATCH
        extra_kwargs = {
            "email": {"read_only": True},
            "username": {"read_only": True},
        }

    def get_avatar_url(self, obj):
        if obj.avatar and hasattr(obj.avatar, "url"):
            try:
                return obj.avatar.url
            except ValueError:
                return None
            except Exception as e:
                print(f"Error getting avatar URL for user {obj.pk}: {e}")
                return None
        return None

    def get_permissions(self, user):
        if not user or not user.is_authenticated:
            return []
        return list(user.get_all_permissions())

    def get_has_business(self, user):
        """Checks if the user owns or manages any active BusinessInfo."""
        if not user or not user.is_authenticated:
            return False
        if BusinessInfo is None:
            print(
                "Warning: BusinessInfo model not available in get_has_business."
            )  # Use logger
            return False
        # Use Exists for efficiency
        return BusinessInfo.objects.filter(
            Q(owner=user) | Q(managers=user),
        ).exists()

    def update(self, instance, validated_data):
        avatar_file = validated_data.pop("avatar", "NOT_PROVIDED")

        # Update other fields first
        instance = super().update(instance, validated_data)

        try:
            if avatar_file is None:
                if instance.avatar:
                    instance.avatar.delete(save=False)  # Delete file from S3
                    instance.avatar = None
                    instance.save(update_fields=["avatar"])
            elif isinstance(
                avatar_file, InMemoryUploadedFile
            ):  # Check if it's a new file
                if instance.avatar:
                    instance.avatar.delete(save=False)  # Delete old file first
                instance.avatar = avatar_file
                instance.save(update_fields=["avatar"])

        except Exception as e:
            print(f"ERROR: Could not process avatar update for user {instance.pk}: {e}")

        return instance


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    user = CustomUserDetailsSerializer(read_only=True)

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        user_serializer = CustomUserDetailsSerializer(self.user, context=self.context)
        data["user"] = user_serializer.data
        return data


class CustomRegisterSerializer(RegisterSerializer):
    first_name = serializers.CharField(required=True, max_length=150)
    last_name = serializers.CharField(required=True, max_length=150)
    birth_date = serializers.DateField(required=False, allow_null=True)
    phone_number = serializers.CharField(
        required=False, allow_blank=True, max_length=20
    )
    bio = serializers.CharField(required=False, allow_blank=True)
    country = serializers.CharField(required=False, allow_blank=True, max_length=100)
    state = serializers.CharField(required=False, allow_blank=True, max_length=100)
    city = serializers.CharField(required=False, allow_blank=True, max_length=100)
    address = serializers.CharField(required=False, allow_blank=True, max_length=255)
    zipCode = serializers.CharField(required=False, allow_blank=True, max_length=20)
    avatar = serializers.ImageField(required=False, allow_null=True)
    user_timezone = serializers.CharField(
        required=False, allow_blank=True, max_length=50
    )

    def get_cleaned_data(self):
        data = super().get_cleaned_data()
        data.update(
            {
                "first_name": self.validated_data.get("first_name", ""),
                "last_name": self.validated_data.get("last_name", ""),
                "birth_date": self.validated_data.get("birth_date", None),
                "phone_number": self.validated_data.get("phone_number", ""),
                "bio": self.validated_data.get("bio", ""),
                "country": self.validated_data.get("country", ""),
                "state": self.validated_data.get("state", ""),
                "city": self.validated_data.get("city", ""),
                "address": self.validated_data.get("address", ""),
                "zipCode": self.validated_data.get("zipCode", ""),
                "avatar": self.validated_data.get("avatar", None),
                "user_timezone": self.validated_data.get("user_timezone", "UTC"),
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
        user.country = self.validated_data.get("country", "")
        user.state = self.validated_data.get("state", "")
        user.city = self.validated_data.get("city", "")
        user.address = self.validated_data.get("address", "")
        user.zipCode = self.validated_data.get("zipCode", "")
        user.avatar = self.validated_data.get("avatar", None)
        user.user_timezone = self.validated_data.get("user_timezone", "UTC")

        if not user.role:
            try:
                default_role, created = Role.objects.get_or_create(
                    name="Student",
                    defaults={
                        "is_default": True,
                        "is_system": False,
                        "hierarchy_level": 10,
                        "color": "#6c757d",
                    },
                )
                if created:
                    print(f"INFO: Default role 'Student' created automatically.")
                user.role = default_role
            except Exception as e:
                print(
                    f"ERROR: Could not assign default role 'Student' during registration for user {user.email}: {e}"
                )

        user.save()
        return user
