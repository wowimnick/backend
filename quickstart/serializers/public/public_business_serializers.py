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
        fields = [
            "businessId",
            "businessName",
            "businessType",
            "businessDescription",
            "business_image_medium_url",
            "website",
            "social_media_links",
            "business_timezone",
            "openingTime",
            "closingTime",
            "studentContactPhone",
            "studentContactEmail",
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

    def get_business_image_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")

    def to_representation(self, instance):
        """
        Modify the serialized data before it's returned.
        This is where we'll remove sensitive contact info based on privacy settings.
        """
        # Get the default serialized representation
        representation = super().to_representation(instance)

        # Check the privacy setting on the model instance
        if instance.contact_privacy != "public":
            representation.pop("studentContactPhone", None)
            representation.pop("studentContactEmail", None)
            representation.pop("website", None)

        return representation

    def get_average_rating(self, obj):
        avg = Reviews.objects.filter(businessId=obj, status="approved").aggregate(
            Avg("rating")
        )["rating__avg"]
        return round(avg, 1) if avg else 0.0
