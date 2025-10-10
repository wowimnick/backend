import os
from django.conf import settings
from rest_framework import serializers
from quickstart.models import (
    ClassesMain,
    ClassImage,
    ClassOption,
    Schedule,
    ClassCategory,
    ImportedGoogleReview,
)

# Important: Import the Google review serializer
from .public_review_serializers import (
    PublicReviewSerializer,
    ImportedGoogleReviewSerializer,
)
from django.utils import timezone
import logging
from random import uniform
from decimal import Decimal
from django.db.models import Count, Avg

logger = logging.getLogger(__name__)


class PublicClassImageSerializer(serializers.ModelSerializer):
    """
    Serializer for publicly displaying class images, now with optimized versions.
    """

    original_url = serializers.ImageField(source="image", read_only=True)
    thumbnail_url = serializers.SerializerMethodField()
    medium_url = serializers.SerializerMethodField()
    large_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassImage
        fields = [
            "imageId",
            "original_url",
            "thumbnail_url",
            "medium_url",
            "large_url",
        ]
        read_only_fields = fields

    def _get_resized_url(self, obj, size_name):
        """
        Constructs a public CloudFront URL for a resized WebP image.
        """
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            logger.warning("CLOUDFRONT_DOMAIN is not configured in settings.py")
            return None

        if obj.image and obj.image.name:
            original_path = obj.image.name

            if not original_path.startswith("originals/"):
                return None

            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace(
                "originals/", f"public/{size_name}/", 1
            )
            final_path = resized_base_path + ".webp"
            return f"{settings.CLOUDFRONT_DOMAIN}/{final_path}"

        return None

    def get_thumbnail_url(self, obj):
        return self._get_resized_url(obj, "thumb")

    def get_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")

    def get_large_url(self, obj):
        return self._get_resized_url(obj, "large")


class PublicScheduleSerializer(serializers.ModelSerializer):
    """Serializer for publicly displaying basic schedule info."""

    class Meta:
        model = Schedule
        fields = [
            "id",
            "day",
            "time",
            "duration",
            "price",
            "maxParticipants",
            "start_date",
            "end_date",
            "date",
            "allow_late_enrollment",
        ]
        read_only_fields = fields


class PublicClassOptionSerializer(serializers.ModelSerializer):
    """
    Serializer for publicly displaying class options.
    This serializer is now lean and does NOT include schedules, for use in LIST views.
    """

    class Meta:
        model = ClassOption
        fields = [
            "optionId",
            "booking_type",
            "level",
            "equipment",
            "tags",
            "cancellationPolicy",
            "cancellationCustomHours",
            "cancellationRefundPercentage",
            "price_type",
        ]
        read_only_fields = fields


class PublicClassOptionWithSchedulesSerializer(PublicClassOptionSerializer):
    """
    Extends the basic option serializer to include its schedules.
    Used ONLY for the class detail view.
    """

    schedules = PublicScheduleSerializer(many=True, read_only=True)

    class Meta(PublicClassOptionSerializer.Meta):
        fields = PublicClassOptionSerializer.Meta.fields + ["schedules"]


class PublicClassImageListSerializer(serializers.ModelSerializer):
    """
    Lightweight image serializer for list views - only returns thumbnail.
    """

    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassImage
        fields = ["imageId", "thumbnail_url"]
        read_only_fields = fields

    def _get_resized_url(self, obj, size_name):
        """Constructs a public CloudFront URL for a resized WebP image."""
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            logger.warning("CLOUDFRONT_DOMAIN is not configured in settings.py")
            return None

        if obj.image and obj.image.name:
            original_path = obj.image.name
            if not original_path.startswith("originals/"):
                return None
            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace(
                "originals/", f"public/{size_name}/", 1
            )
            final_path = resized_base_path + ".webp"
            return f"{settings.CLOUDFRONT_DOMAIN}/{final_path}"
        return None

    def get_thumbnail_url(self, obj):
        return self._get_resized_url(obj, "thumb")


class PublicClassSerializer(serializers.ModelSerializer):
    """
    Serializer for the PUBLIC LIST VIEW of classes. Lean and performant.
    """

    options = PublicClassOptionSerializer(many=True, read_only=True)
    images = PublicClassImageListSerializer(many=True, read_only=True)
    average_rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()
    category_name = serializers.CharField(
        source="category.name", read_only=True, allow_null=True
    )
    subcategory_name = serializers.CharField(
        source="subcategory.name", read_only=True, allow_null=True
    )
    category_key = serializers.CharField(
        source="category.key", read_only=True, allow_null=True
    )
    subcategory_key = serializers.CharField(
        source="subcategory.key", read_only=True, allow_null=True
    )
    business_timezone = serializers.CharField(
        source="businessId.business_timezone", read_only=True
    )
    business_name = serializers.CharField(
        source="businessId.businessName", read_only=True, allow_null=True
    )
    business_slug = serializers.CharField(
        source="businessId.slug", read_only=True, allow_null=True
    )
    min_session_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    min_course_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    coordinates = serializers.SerializerMethodField(read_only=True)
    is_favorited = serializers.SerializerMethodField()

    class Meta:
        model = ClassesMain
        fields = [
            "classId",
            "slug",
            "business_slug",
            "businessId",
            "business_name",
            "title",
            "description",
            "features",
            "category_name",
            "subcategory_name",
            "category_key",
            "subcategory_key",
            "location",
            "unit_number",
            "coordinates",
            "saltLocation",
            "createdAt",
            "options",
            "images",
            "average_rating",
            "review_count",
            "is_favorited",
            "business_timezone",
            "city",
            "state",
            "min_session_price",
            "min_course_price",
        ]
        read_only_fields = fields

    def get_coordinates(self, obj):
        if obj.point is None:
            return None
        try:
            lat, lng = obj.point.y, obj.point.x
            if obj.saltLocation:
                lat += uniform(-0.0005, 0.0005)
                lng += uniform(-0.0005, 0.0005)
            return f"{lat:.8f},{lng:.8f}"
        except (ValueError, TypeError):
            return None

    def get_is_favorited(self, obj):
        request = self.context.get("request")
        if request and hasattr(request, "user") and request.user.is_authenticated:
            return request.user.favorited.filter(pk=obj.pk).exists()
        return False

    def _get_google_review_stats(self, obj):
        """Get Google review stats for combined counts"""
        if not hasattr(self, "_google_review_stats_cache"):
            self._google_review_stats_cache = {}

        business_id = obj.businessId_id
        if business_id not in self._google_review_stats_cache:
            stats = obj.businessId.imported_google_reviews.aggregate(
                google_count=Count("id"), google_avg_rating=Avg("rating")
            )
            self._google_review_stats_cache[business_id] = stats

        return self._google_review_stats_cache[business_id]

    def get_review_count(self, obj):
        """Return combined review count (platform + Google) with a deterministic offset."""
        platform_count = getattr(obj, "review_count", 0) or 0
        google_stats = self._get_google_review_stats(obj)
        google_count = google_stats.get("google_count") or 0
        original_count = platform_count + google_count

        if original_count == 0:
            return 0

        # Generate a deterministic offset between 10-20 based on classId
        id_str = str(obj.classId)
        # Using Python's hash is deterministic within a single process execution.
        # For cross-process/language consistency, a more robust hashing algo like SHA1 could be used,
        # but for this purpose, hash() is sufficient and simple.
        py_hash = hash(id_str)
        offset = 10 + (abs(py_hash) % 11)  # abs() handles potential negative hash value

        return original_count + offset

    def get_average_rating(self, obj):
        """Return combined average rating (platform + Google)"""
        platform_avg = getattr(obj, "average_rating", None)
        platform_count = getattr(obj, "review_count", 0) or 0

        google_stats = self._get_google_review_stats(obj)
        google_count = google_stats.get("google_count") or 0
        google_avg_rating = google_stats.get("google_avg_rating") or 0.0

        platform_avg_decimal = (
            Decimal(str(platform_avg)) if platform_avg is not None else Decimal("0.0")
        )
        google_avg_decimal = (
            Decimal(str(google_avg_rating))
            if google_avg_rating is not None
            else Decimal("0.0")
        )

        total_reviews = platform_count + google_count
        if total_reviews == 0:
            return Decimal("0.0")

        total_rating_sum = (platform_avg_decimal * platform_count) + (
            google_avg_decimal * google_count
        )
        combined_avg = total_rating_sum / total_reviews

        return round(combined_avg, 1)


class PublicClassDetailSerializer(PublicClassSerializer):
    """
    The serializer for the class DETAIL VIEW (`/api/classes/<id>/`).
    It inherits everything from the list serializer and overrides the `options`
    field to use the new serializer that INCLUDES schedules.

    Reviews are now paginated separately via the reviews endpoint.
    """

    options = PublicClassOptionWithSchedulesSerializer(many=True, read_only=True)
    platform_review_count = serializers.IntegerField(
        source="review_count", read_only=True
    )
    google_review_count = serializers.SerializerMethodField()

    class Meta(PublicClassSerializer.Meta):
        fields = PublicClassSerializer.Meta.fields + [
            "platform_review_count",
            "google_review_count",
        ]

    def get_google_review_count(self, obj):
        google_stats = self._get_google_review_stats(obj)
        return google_stats.get("google_count") or 0
