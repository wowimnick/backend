# --- START OF FILE quickstart/serializers/public/public_class_serializers.py ---

import os
from django.conf import settings
from rest_framework import serializers
from quickstart.models import (
    ClassesMain,
    ClassImage,
    ClassOption,
    Schedule,
    ClassCategory,
)
from django.utils import timezone
import logging
from random import uniform  # For coordinate salting if needed here

logger = logging.getLogger(__name__)


class PublicClassImageSerializer(serializers.ModelSerializer):
    """
    Serializer for publicly displaying class images, now with optimized versions.
    """

    # This correctly uses your PrivateMediaStorage to generate a pre-signed URL for the original.
    original_url = serializers.ImageField(source="image", read_only=True)

    # These methods will now build the correct CloudFront URLs.
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

            # 1. Get the base path of the original image, without its extension
            base_path, _ = os.path.splitext(
                original_path
            )  # e.g., "originals/path/image.png" -> "originals/path/image"

            # 2. Replace the path prefix
            # e.g., "originals/path/image" -> "public/thumb/path/image"
            resized_base_path = base_path.replace(
                "originals/", f"public/{size_name}/", 1
            )

            # 3. Add the correct .webp extension
            final_path = resized_base_path + ".webp"

            # 4. Construct the full URL
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
            "cancellationRefundPercentage",
            "price_type",
        ]
        read_only_fields = fields


# --- NEW SERIALIZER FOR DETAIL VIEW ---
class PublicClassOptionWithSchedulesSerializer(PublicClassOptionSerializer):
    """
    Extends the basic option serializer to include its schedules.
    Used ONLY for the class detail view.
    """

    schedules = PublicScheduleSerializer(many=True, read_only=True)

    class Meta(PublicClassOptionSerializer.Meta):
        # Inherit fields and add 'schedules'
        fields = PublicClassOptionSerializer.Meta.fields + ["schedules"]


class PublicClassSerializer(serializers.ModelSerializer):
    """
    Serializer for the PUBLIC LIST VIEW of classes. Lean and performant.
    It does NOT include schedules to keep the payload small.
    """

    options = PublicClassOptionSerializer(many=True, read_only=True)
    images = PublicClassImageSerializer(many=True, read_only=True)

    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)

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

    coordinates = serializers.SerializerMethodField(read_only=True)
    is_favorited = serializers.SerializerMethodField()
    business_timezone = serializers.CharField(
        source="businessId.business_timezone", read_only=True
    )
    business_name = serializers.CharField(
        source="businessId.businessName", read_only=True, allow_null=True
    )

    min_session_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    min_course_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )

    class Meta:
        model = ClassesMain
        fields = [
            "classId",
            "businessId",
            "business_name",
            "title",
            "description",
            "features",
            "category_name",
            "subcategory_name",
            "category_key",
            "subcategory_key",
            "coordinates",
            "saltLocation",
            "createdAt",
            "options",
            "images",
            "average_rating",
            "review_count",
            "is_favorited",
            "business_timezone",
            "min_session_price",
            "min_course_price",
        ]
        read_only_fields = fields

    def get_coordinates(self, obj):
        if obj.latitude is None or obj.longitude is None:
            return None

        try:
            lat, lng = float(obj.latitude), float(obj.longitude)
            if obj.saltLocation:
                lat_salt = uniform(-0.0005, 0.0005)
                lng_salt = uniform(-0.0005, 0.0005)
                lat += lat_salt
                lng += lng_salt
            return f"{lat:.8f},{lng:.8f}"
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid numeric coordinates for Class {obj.classId}: lat={obj.latitude}, lng={obj.longitude}"
            )
            return None

    def get_is_favorited(self, obj):
        request = self.context.get("request")
        if request and hasattr(request, "user") and request.user.is_authenticated:
            return request.user.favorited.filter(pk=obj.pk).exists()
        return False


class PublicClassDetailSerializer(PublicClassSerializer):
    """
    The serializer for the class DETAIL VIEW (`/api/classes/<id>/`).
    It inherits everything from the list serializer and overrides the `options`
    field to use the new serializer that INCLUDES schedules.
    """

    options = PublicClassOptionWithSchedulesSerializer(many=True, read_only=True)

    class Meta(PublicClassSerializer.Meta):
        # The fields are inherited, so we don't need to redeclare them.
        # The override of the `options` field above is all that's needed.
        pass
