from django.conf import settings
from rest_framework import serializers
from quickstart.models import CustomUser


class MyProfileSerializer(serializers.ModelSerializer):
    """Serializer for a user viewing their OWN profile with optimized avatar URLs."""

    avatar_thumb_url = serializers.SerializerMethodField()
    avatar_medium_url = serializers.SerializerMethodField()
    # The original can still be useful for a profile picture editor
    avatar_original_url = serializers.ImageField(
        source="avatar", read_only=True, use_url=True
    )

    role_name = serializers.CharField(
        source="role.name", read_only=True, default="Student"
    )

    class Meta:
        model = CustomUser
        fields = [
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
            "avatar_thumb_url",
            "avatar_medium_url",
            "avatar_original_url",
            "role_name",
            "createdAt",
            "last_login",
        ]
        read_only_fields = [
            "userId",
            "email",
            "username",
            "avatar_thumb_url",
            "avatar_medium_url",
            "avatar_original_url",
            "role_name",
            "createdAt",
            "last_login",
        ]

    def _get_resized_avatar_url(self, obj, size_name):
        if not obj.avatar or not obj.avatar.name:
            return None

        original_path = obj.avatar.name
        if not original_path.startswith("originals/"):
            return None

        resized_path = original_path.replace("originals/", f"public/{size_name}/", 1)

        # Prepend the domain from settings
        return f"{settings.CLOUDFRONT_DOMAIN}/{resized_path}"

    def get_avatar_thumb_url(self, obj):
        return self._get_resized_avatar_url(obj, "thumb")

    def get_avatar_medium_url(self, obj):
        return self._get_resized_avatar_url(obj, "medium")
