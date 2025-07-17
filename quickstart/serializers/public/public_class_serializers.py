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
    """Serializer for publicly displaying class options."""

    schedules = serializers.SerializerMethodField()

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
            "schedules",
            "price_type",
        ]
        read_only_fields = fields

    def get_schedules(self, option_instance: ClassOption):
        """
        Returns only active and future schedules for the given class option.
        This relies on prefetching in the ViewSet to be efficient.
        """
        today = timezone.now().date()

        # Access schedules related to the option_instance.
        active_schedules = option_instance.schedules.all()

        future_schedules_objects = []
        for schedule_obj in active_schedules:
            is_future = False
            # Logic for course or single session based on parent option booking_type
            if option_instance.booking_type == "Full Course":
                if schedule_obj.end_date and schedule_obj.end_date >= today:
                    is_future = True
            else:  # Single Session
                if schedule_obj.date and schedule_obj.date >= today:
                    is_future = True

            if is_future:
                future_schedules_objects.append(schedule_obj)

        # Serialize only the filtered future schedules
        return PublicScheduleSerializer(
            future_schedules_objects, many=True, context=self.context
        ).data


class PublicClassSerializer(serializers.ModelSerializer):
    """Serializer for public listing and detail view of classes."""

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
    # FIX: Add business_name from the related Business model.
    # The `source` points to the `businessName` field on the `Business` model,
    # and it will be serialized as `business_name` in the JSON response.
    business_name = serializers.CharField(
        source="businessId.businessName", read_only=True, allow_null=True
    )

    class Meta:
        model = ClassesMain
        fields = [
            "classId",
            "businessId",
            "business_name",  # FIX: Added business_name to the list of fields.
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
        ]
        read_only_fields = fields

    def get_coordinates(self, obj):
        if not obj.coordinates:
            return None
        try:
            lat, lng = map(float, obj.coordinates.split(","))
            if obj.saltLocation:
                lat_salt = uniform(-0.0005, 0.0005)
                lng_salt = uniform(-0.0005, 0.0005)
                lat += lat_salt
                lng += lng_salt
            return f"{lat:.8f},{lng:.8f}"
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid public coordinates format for Class {obj.classId}: {obj.coordinates}"
            )
            return None

    def get_is_favorited(self, obj):
        request = self.context.get("request")
        if request and hasattr(request, "user") and request.user.is_authenticated:
            return request.user.favorited.filter(pk=obj.pk).exists()
        return False

    def get_business_image(self, obj):
        if (
            obj.businessId
            and hasattr(obj.businessId, "businessImage")
            and obj.businessId.businessImage
        ):
            try:
                return obj.businessId.businessImage.url
            except ValueError:
                return None
        return None
