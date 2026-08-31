import os
from django.conf import settings
from rest_framework import serializers

from quickstart.utils.url_utils import build_cloudfront_url
from quickstart.utils.business_location_utils import (
    business_location_identity_key_from_instance,
)
from quickstart.models import BusinessInfo, BusinessLocation, Reviews, ImportedGoogleReview
from .public_class_serializers import HomepageClassSerializer
from .public_review_serializers import (
    PublicReviewSerializer,
    ImportedGoogleReviewSerializer,
)
from django.db.models import Avg, Count
from decimal import Decimal


class BusinessContactDetailSerializer(serializers.ModelSerializer):
    """
    A secure serializer that ONLY exposes contact details for users
    who are authorized to see them (i.e., after booking).
    """

    class Meta:
        model = BusinessInfo
        fields = [
            "studentContactPhone",
            "studentContactEmail",
            "website",
            "businessUnit",
        ]
        read_only_fields = fields


class PublicBusinessLocationSerializer(serializers.ModelSerializer):
    """Active business venues for the public business page."""

    class Meta:
        model = BusinessLocation
        fields = [
            "id",
            "name",
            "address",
            "unit",
            "city",
            "state",
            "zip_code",
            "latitude",
            "longitude",
            "show_exact_location",
            "is_primary",
        ]
        read_only_fields = fields


class PublicBusinessInfoSerializer(serializers.ModelSerializer):
    """Serializer for PUBLIC display of Business Information."""

    # These fields are now handled by the detail serializer or are properties
    average_rating = serializers.DecimalField(
        max_digits=3, decimal_places=1, read_only=True
    )
    totalReviews = serializers.IntegerField(
        source="total_reviews_count", read_only=True
    )
    business_image_medium_url = serializers.SerializerMethodField()
    partner_tier_name = serializers.CharField(
        source="partner_tier.name", read_only=True, allow_null=True
    )
    instagram_follower_count = serializers.IntegerField(read_only=True, allow_null=True)
    instagram_followers_synced_at = serializers.DateTimeField(read_only=True, allow_null=True)
    instagram_sync_status = serializers.CharField(read_only=True)

    class Meta:
        model = BusinessInfo
        fields = [
            "businessId",
            "businessName",
            "slug",
            "businessType",
            "businessDescription",
            "business_image_medium_url",
            "website",
            "social_media_links",
            "business_timezone",
            "businessHours",
            "studentContactPhone",
            "studentContactEmail",
            "businessAddress",
            "businessUnit",
            "businessCity",
            "businessState",
            "totalReviews",  # This will now be the combined count on the detail view
            "average_rating",  # This will now be the combined rating on the detail view
            "featured",
            "contact_privacy",
            "founding_year",
            "createdAt",
            "partner_tier_name",
            "instagram_follower_count",
            "instagram_followers_synced_at",
            "instagram_sync_status",
        ]
        read_only_fields = fields

    def _get_resized_url(self, obj, size_name):
        """
        Constructs a public CloudFront URL for a resized WebP image.
        """
        if obj.businessImage and obj.businessImage.name:
            original_path = obj.businessImage.name
            if not original_path.startswith("originals/"):
                return None
            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace(
                "originals/", f"public/{size_name}/", 1
            )
            final_path = resized_base_path + ".webp"
            return build_cloudfront_url(final_path)
        return None

    def get_business_image_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")

    def to_representation(self, instance):
        """
        Modify the serialized data before it's returned.
        This is where we'll remove sensitive contact info based on privacy settings.
        """
        representation = super().to_representation(instance)
        if instance.contact_privacy != "public":
            representation.pop("studentContactPhone", None)
            representation.pop("studentContactEmail", None)
            representation.pop("website", None)
            representation.pop("businessUnit", None)
        return representation


class PublicBusinessDetailSerializer(PublicBusinessInfoSerializer):
    """
    A detailed serializer for the standalone business page, including
    all *active* classes and now combining platform and Google reviews.
    """

    classes = HomepageClassSerializer(many=True, read_only=True, source="active_classes")
    locations = serializers.SerializerMethodField()

    # Overwrite fields from parent to use combined metrics
    totalReviews = serializers.SerializerMethodField(
        method_name="get_combined_review_count"
    )
    average_rating = serializers.SerializerMethodField(
        method_name="get_combined_average_rating"
    )

    class Meta(PublicBusinessInfoSerializer.Meta):
        fields = PublicBusinessInfoSerializer.Meta.fields + [
            "classes",
            "locations",
            "widget_api_key",  # for hosted join page widget embed
        ]

    def get_locations(self, obj):
        prefetched = getattr(obj, "_prefetched_objects_cache", {}).get("locations")
        if prefetched is not None:
            qs = [loc for loc in prefetched if getattr(loc, "is_active", True)]
        else:
            qs = list(
                obj.locations.filter(is_active=True).order_by("-is_primary", "name")
            )
        seen = set()
        unique = []
        for loc in qs:
            key = business_location_identity_key_from_instance(loc)
            if key[0] == "none":
                key = ("id", str(loc.pk))
            if key in seen:
                continue
            seen.add(key)
            unique.append(loc)
        return PublicBusinessLocationSerializer(unique, many=True).data

    def _get_google_review_stats(self, obj):
        return {
            "google_count": obj.google_review_count or 0,
            "google_avg_rating": obj.google_avg_rating or Decimal("0.0"),
        }

    def get_combined_review_count(self, obj):
        platform_count = obj.total_reviews_count
        google_stats = self._get_google_review_stats(obj)
        google_count = google_stats.get("google_count") or 0
        return platform_count + google_count

    def get_combined_average_rating(self, obj):
        platform_count = obj.total_reviews_count
        platform_avg = obj.average_rating or Decimal("0.0")

        google_stats = self._get_google_review_stats(obj)
        google_count = google_stats.get("google_count") or 0
        google_avg = google_stats.get("google_avg_rating") or Decimal("0.0")

        total_reviews = platform_count + google_count
        if total_reviews == 0:
            return Decimal("0.0")

        total_rating_sum = (platform_avg * platform_count) + (
            Decimal(google_avg) * google_count
        )
        combined_avg = total_rating_sum / total_reviews

        return round(combined_avg, 1)
