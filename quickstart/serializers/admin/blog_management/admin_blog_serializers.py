import os
from django.conf import settings
from rest_framework import serializers

from quickstart.utils.url_utils import build_cloudfront_url
from quickstart.models import BlogCategory, BlogPost, CustomUser

# --- Admin Management Serializers ---


class AdminBlogCategorySerializer(serializers.ModelSerializer):
    post_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = BlogCategory
        fields = ["id", "name", "slug", "post_count"]

    def validate_slug(self, value):
        if not value.islower() or not value.replace("-", "").isalnum():
            raise serializers.ValidationError(
                "Slug must be lowercase and contain only letters, numbers, and hyphens."
            )
        return value


class AdminBlogPostSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.get_full_name", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    author_avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = BlogPost
        fields = [
            "id",
            "title",
            "slug",
            "excerpt",
            "content",
            "image_url",
            "author",
            "author_name",
            "author_avatar_url",
            "category",
            "category_name",
            "tags",
            "status",
            "published_date",
            "read_time",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "author",
            "read_time",
            "created_at",
            "updated_at",
            "author_name",
            "category_name",
            "author_avatar_url",
        ]

    def get_author_avatar_url(self, obj):
        if obj.author and obj.author.avatar and hasattr(obj.author.avatar, "name"):
            original_path = obj.author.avatar.name
            if not original_path.startswith("originals/"):
                return None
            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace("originals/", "public/thumb/", 1)
            webp_path = resized_base_path + ".webp"
            return build_cloudfront_url(webp_path)
        return None

    def create(self, validated_data):
        validated_data["author"] = self.context["request"].user
        return super().create(validated_data)
