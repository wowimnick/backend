# quickstart/serializers/admin/business_management/admin_business_serializers.py

import os
from django.conf import settings
from rest_framework import serializers

from quickstart.utils.url_utils import build_cloudfront_url
from ....models import BusinessInfo, ClassCategory
from decimal import Decimal
from rest_framework_gis.serializers import GeoFeatureModelSerializer
from rest_framework import serializers
from quickstart.models import GeographicBoundary


class GeographicBoundaryDataSerializer(GeoFeatureModelSerializer):
    """
    Serializes GeographicBoundary model into a GeoJSON Feature,
    including annotated business metrics.
    """

    # These fields are expected to be annotated onto the queryset in the view
    business_count = serializers.IntegerField(read_only=True)
    total_revenue = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    class Meta:
        model = GeographicBoundary
        geo_field = "geom"  # Specify the geometry field
        fields = [
            "id",
            "csuid",
            "name",
            "province",
            # Include the annotated fields
            "business_count",
            "total_revenue",
        ]


# --- Admin List Serializer ---
class AdminBusinessListSerializer(serializers.ModelSerializer):
    """
    Serializer for the admin business list view. Optimized for read-only display.
    Includes key identifiers and annotated metrics expected from the ViewSet queryset.
    """

    # --- Related Fields (Read-Only) ---
    owner_email = serializers.EmailField(
        source="owner.email", read_only=True, allow_null=True
    )
    owner_id = serializers.IntegerField(
        source="owner.userId", read_only=True, allow_null=True
    )

    # --- Annotated Fields (Read-Only - Expected from ViewSet's get_queryset) ---
    has_active_schedules = serializers.BooleanField(read_only=True, default=False)
    status = serializers.CharField(read_only=True, default="unknown")
    rating = serializers.FloatField(read_only=True, default=0.0)
    revenue = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, default=Decimal("0.00")
    )
    classes_count = serializers.IntegerField(read_only=True, default=0)
    bookings_count = serializers.IntegerField(read_only=True, default=0)
    review_count = serializers.IntegerField(read_only=True, default=0)
    google_review_count = serializers.IntegerField(read_only=True, default=0)

    # --- Model Fields (Read-Only for List) ---
    business_image_thumb_url = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            "businessId",
            "businessName",
            "businessType",
            "business_image_thumb_url",
            "businessCity",
            "businessState",
            "isActive",
            "featured",
            "createdAt",
            "owner_email",
            "owner_id",
            "has_active_schedules",
            "status",
            "rating",
            "revenue",
            "classes_count",
            "bookings_count",
            "review_count",
            "google_review_count",
            "verificationStatus",
        ]
        read_only_fields = fields

    def get_business_image_thumb_url(self, obj):
        if obj.businessImage and hasattr(obj.businessImage, "name"):
            original_path = obj.businessImage.name
            if not original_path.startswith("originals/"):
                return None
            # Correctly builds the path to the .webp thumbnail
            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace("originals/", "public/thumb/", 1)
            webp_path = resized_base_path + ".webp"
            return build_cloudfront_url(webp_path)
        return None


# --- Admin Detail/Update Serializer ---
class AdminBusinessDetailSerializer(serializers.ModelSerializer):
    """
    Serializer for the admin business detail view (retrieve, update).
    Allows admins to view detailed information and update specific fields.
    """

    owner_email = serializers.EmailField(
        source="owner.email", read_only=True, allow_null=True
    )
    owner_id = serializers.IntegerField(
        source="owner.userId", read_only=True, allow_null=True
    )
    has_active_schedules = serializers.BooleanField(read_only=True, default=False)
    google_review_count = serializers.IntegerField(read_only=True, default=0)
    managers_emails = serializers.SerializerMethodField(read_only=True)
    status = serializers.CharField(read_only=True, default="unknown")
    average_rating = serializers.FloatField(
        source="rating", read_only=True, default=0.0
    )
    revenue = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, default=Decimal("0.00")
    )
    classes_count = serializers.IntegerField(read_only=True, default=0)
    bookings_count = serializers.IntegerField(read_only=True, default=0)
    review_count = serializers.IntegerField(read_only=True, default=0)
    classFormats_list = serializers.SerializerMethodField(read_only=True)
    skillLevels_list = serializers.SerializerMethodField(read_only=True)
    ageGroups_list = serializers.SerializerMethodField(read_only=True)

    business_image_medium_url = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            "businessId",
            "businessName",
            "businessType",
            "business_image_medium_url",
            "businessDescription",
            "createdAt",
            "businessHours",
            "liabilityWaiver",
            "studentContactPhone",
            "studentContactEmail",
            "preferredContact",
            "website",
            "businessAddress",
            "businessCity",
            "businessState",
            "businessZipCode",
            "latitude",
            "longitude",
            "showExactLocation",
            "classFormats",
            "skillLevels",
            "ageGroups",
            "social_media_links",
            "tags_keywords",
            "featured",
            "isActive",
            "verificationStatus",
            "owner_email",
            "owner_id",
            "has_active_schedules",
            "managers_emails",
            "status",
            "average_rating",
            "revenue",
            "classes_count",
            "bookings_count",
            "review_count",
            "google_review_count",
            "classFormats_list",
            "skillLevels_list",
            "ageGroups_list",
        ]
        read_only_fields = [
            "businessId",
            "createdAt",
            "owner_email",
            "owner_id",
            "has_active_schedules",
            "google_review_count",
            "managers_emails",
            "status",
            "average_rating",
            "revenue",
            "classes_count",
            "bookings_count",
            "review_count",
            "classFormats_list",
            "skillLevels_list",
            "ageGroups_list",
        ]

    def get_business_image_medium_url(self, obj):
        """Safely get the medium-sized business image URL."""
        if obj.businessImage and hasattr(obj.businessImage, "name"):
            original_path = obj.businessImage.name
            if not original_path.startswith("originals/"):
                return None
            # Correctly builds the path to the .webp medium image
            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace("originals/", "public/medium/", 1)
            webp_path = resized_base_path + ".webp"
            return build_cloudfront_url(webp_path)
        return None

    def get_classFormats_list(self, obj):
        return obj.classFormats if isinstance(obj.classFormats, list) else []

    def get_skillLevels_list(self, obj):
        return obj.skillLevels if isinstance(obj.skillLevels, list) else []

    def get_ageGroups_list(self, obj):
        return obj.ageGroups if isinstance(obj.ageGroups, list) else []

    def get_managers_emails(self, obj):
        if hasattr(obj, "managers"):
            return [manager.email for manager in obj.managers.all()]
        return []
