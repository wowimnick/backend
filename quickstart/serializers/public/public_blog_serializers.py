# quickstart/serializers/blog_serializers.py
from rest_framework import serializers
from quickstart.models import BlogCategory, BlogPost, CustomUser

# --- Public Facing Serializers ---


class PublicBlogAuthorSerializer(serializers.ModelSerializer):
    avatarUrl = serializers.ImageField(source="avatar", read_only=True, use_url=True)
    name = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ["name", "avatarUrl"]

    def get_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()


class PublicBlogCategorySerializer(serializers.ModelSerializer):
    """Sidebar/categories API: `post_count` comes from queryset annotate. Nested on posts: omitted (null)."""

    post_count = serializers.SerializerMethodField()

    class Meta:
        model = BlogCategory
        fields = ["name", "slug", "post_count"]

    def get_post_count(self, obj):
        return getattr(obj, "post_count", None)


class PublicBlogPostListSerializer(serializers.ModelSerializer):
    category = PublicBlogCategorySerializer(read_only=True)
    author = PublicBlogAuthorSerializer(read_only=True)
    imageUrl = serializers.URLField(source="image_url")
    publishedDate = serializers.DateTimeField(source="published_date")
    readTime = serializers.IntegerField(source="read_time")

    class Meta:
        model = BlogPost
        fields = [
            "slug",
            "title",
            "excerpt",
            "imageUrl",
            "author",
            "publishedDate",
            "category",
            "readTime",
            "tags",
        ]


class PublicBlogPostDetailSerializer(PublicBlogPostListSerializer):
    class Meta(PublicBlogPostListSerializer.Meta):
        fields = PublicBlogPostListSerializer.Meta.fields + ["content"]
