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
        ]
        read_only_fields = fields

    def get_business_image_medium_url(self, obj):
        if not obj.businessImage or not obj.businessImage.name:
            return None
        original_path = obj.businessImage.name
        if not original_path.startswith("originals/"):
            return None
        resized_path = original_path.replace("originals/", "public/medium/", 1)
        return f"{settings.CLOUDFRONT_DOMAIN}/{resized_path}"

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
