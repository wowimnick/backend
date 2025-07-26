from rest_framework import serializers
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

    class Meta:
        model = BlogPost
        fields = "__all__"
        read_only_fields = [
            "author",
            "read_time",
            "created_at",
            "updated_at",
            "author_name",
            "category_name",
        ]

    def create(self, validated_data):
        validated_data["author"] = self.context["request"].user
        return super().create(validated_data)
