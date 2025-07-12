# quickstart/serializers/admin/business_management/admin_business_serializers.py

from rest_framework import serializers
from ....models import BusinessInfo, ClassCategory
from decimal import Decimal


# --- Helper Function ---
def _split_string_to_list(data_string):
    """Helper to split a comma-separated string into a list of strings."""
    if data_string and isinstance(data_string, str):
        return [item.strip() for item in data_string.split(",") if item.strip()]
    return []


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

    # --- Annotated Fields (Read-Only - Expected from ViewSet's get_queryset) ---
    status = serializers.CharField(read_only=True, default="unknown")
    rating = serializers.FloatField(read_only=True, default=0.0)
    revenue = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, default=Decimal("0.00")
    )
    classes_count = serializers.IntegerField(read_only=True, default=0)
    bookings_count = serializers.IntegerField(read_only=True, default=0)
    review_count = serializers.IntegerField(read_only=True, default=0)

    # --- Model Fields (Read-Only for List) ---
    # FIX: Use a SerializerMethodField to safely generate the image URL.
    businessImage = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            "businessId",
            "businessName",
            "businessType",
            "businessImage",  # This will now use the method field
            "businessCity",
            "businessState",
            "isActive",
            "featured",
            "createdAt",
            "owner_email",
            "status",
            "rating",
            "revenue",
            "classes_count",
            "bookings_count",
            "review_count",
            "verificationStatus",
        ]
        read_only_fields = fields

    def get_businessImage(self, obj):
        """
        Safely get the business image URL.
        Returns the URL if the image exists, otherwise returns None.
        """
        if obj.businessImage and hasattr(obj.businessImage, "url"):
            return obj.businessImage.url
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

    # FIX: Use a SerializerMethodField for businessImage here as well.
    businessImage = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            "businessId",
            "businessName",
            "businessType",
            "businessImage",
            "businessDescription",
            "createdAt",
            "openingTime",
            "closingTime",
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
        read_only_fields = [
            "businessId",
            "createdAt",
            "owner_email",
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

    def get_businessImage(self, obj):
        """
        Safely get the business image URL for the detail view.
        """
        if obj.businessImage and hasattr(obj.businessImage, "url"):
            return obj.businessImage.url
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
