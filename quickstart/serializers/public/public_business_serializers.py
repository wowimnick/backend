# In quickstart/serializers/public/public_business_serializers.py

import os
from django.conf import settings
from rest_framework import serializers
from quickstart.models import BusinessInfo, ClassesMain, Reviews
from django.db.models import Avg


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
            "businessUnit",  # Added businessUnit
        ]
        read_only_fields = fields


class PublicBusinessInfoSerializer(serializers.ModelSerializer):
    """Serializer for PUBLIC display of Business Information."""

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

    class Meta:
        model = BusinessInfo
        # MODIFIED: Replaced openingTime/closingTime with businessHours and added businessUnit
        fields = [
            "businessId",
            "businessName",
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
            "totalReviews",
            "average_rating",
            "featured",
            "contact_privacy",
            "founding_year",
            "createdAt",
            "partner_tier_name",
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
            return f"{settings.CLOUDFRONT_DOMAIN}/{final_path}"
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
            representation.pop(
                "businessUnit", None
            )  # Also hide unit if contact is private
        return representation

    def get_average_rating(self, obj):
        avg = Reviews.objects.filter(businessId=obj, status="approved").aggregate(
            Avg("rating")
        )["rating__avg"]
        return round(avg, 1) if avg else 0.0
