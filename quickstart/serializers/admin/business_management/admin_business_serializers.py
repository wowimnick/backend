# --- START OF FILE admin_business_serializers.py ---

from rest_framework import serializers
from ....models import BusinessInfo, ClassCategory
from decimal import Decimal


# --- Helper Function ---
def _split_string_to_list(data_string):
    """Helper to split a comma-separated string into a list of strings."""
    if data_string and isinstance(data_string, str):
        return [item.strip() for item in data_string.split(',') if item.strip()]
    return []

# --- Admin List Serializer ---
class AdminBusinessListSerializer(serializers.ModelSerializer):
    """
    Serializer for the admin business list view. Optimized for read-only display.
    Includes key identifiers and annotated metrics expected from the ViewSet queryset.
    """
    # --- Related Fields (Read-Only) ---
    owner_email = serializers.EmailField(source='owner.email', read_only=True, allow_null=True)

    # --- Annotated Fields (Read-Only - Expected from ViewSet's get_queryset) ---
    # Provide default values for safety, although annotations should ideally exist.
    status = serializers.CharField(read_only=True, default='unknown')
    rating = serializers.FloatField(read_only=True, default=0.0)
    revenue = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=Decimal('0.00'))
    classes_count = serializers.IntegerField(read_only=True, default=0)
    bookings_count = serializers.IntegerField(read_only=True, default=0)
    review_count = serializers.IntegerField(read_only=True, default=0)

    # --- Model Fields (Read-Only for List) ---
    businessImage = serializers.ImageField(read_only=True, use_url=True)
    classCategory = serializers.CharField(source='classCategory.key', read_only=True, allow_null=True)

    class Meta:
        model = BusinessInfo
        fields = [
            # Identifiers & Basic Info
            'businessId',
            'businessName',
            'businessType',
            'businessImage',    # Image URL
            'businessCity',
            'businessState',
            'classCategory',    # Category key/name
            'featured',         # Boolean flag
            'createdAt',        # Creation timestamp

            # Related Info
            'owner_email',      # Owner's email

            # Annotated Metrics & Status
            'status',
            'rating',
            'revenue',
            'classes_count',
            'bookings_count',
            'review_count',
        ]
        # All fields are read-only in the list view context.
        # Explicitly listing them is clearer than `read_only_fields = fields`.
        read_only_fields = [
            'businessId', 'businessName', 'businessType', 'businessImage',
            'businessCity', 'businessState', 'classCategory', 'featured',
            'createdAt', 'owner_email', 'status', 'rating',
            'revenue', 'classes_count', 'bookings_count', 'review_count',
        ]

# --- Admin Detail/Update Serializer ---
class AdminBusinessDetailSerializer(serializers.ModelSerializer):
    """
    Serializer for the admin business detail view (retrieve, update).
    Allows admins to view detailed information and update specific fields.
    """
    owner_email = serializers.EmailField(source='owner.email', read_only=True, allow_null=True)
    managers_emails = serializers.SerializerMethodField(read_only=True)
    status = serializers.CharField(read_only=True, default='unknown')
    rating = serializers.FloatField(read_only=True, default=0.0)
    revenue = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=Decimal('0.00'))
    classes_count = serializers.IntegerField(read_only=True, default=0)
    bookings_count = serializers.IntegerField(read_only=True, default=0)
    review_count = serializers.IntegerField(read_only=True, default=0)
    subcategories_list = serializers.SerializerMethodField(read_only=True)
    classFormats_list = serializers.SerializerMethodField(read_only=True)
    skillLevels_list = serializers.SerializerMethodField(read_only=True)
    ageGroups_list = serializers.SerializerMethodField(read_only=True)
    classCategory_key = serializers.CharField(source='classCategory.key', read_only=True, allow_null=True)

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'businessType', 'businessImage',
            'businessDescription', 'createdAt', 'openingTime', 'closingTime',
            'liabilityWaiver', 'studentContactPhone', 'studentContactEmail',
            'preferredContact', 'website', 'businessAddress', 'businessCity',
            'businessState', 'businessZipCode', 'latitude', 'longitude',
            'showExactLocation', 'classCategory', 'classCategory_key', # Added key for reading
            'subcategories',
            'classFormats', 'skillLevels', 'ageGroups', 'social_media_links',
            'tags_keywords', 'featured', 'isActive', 'verificationStatus',
            'owner_email', 'managers_emails', 'status', 'rating', 'revenue',
            'classes_count', 'bookings_count', 'review_count',
            'subcategories_list', 'classFormats_list', 'skillLevels_list',
            'ageGroups_list',
        ]

        read_only_fields = [
            'businessId', 'createdAt', 'verificationStatus', 'owner_email',
            'managers_emails', 'status', 'rating', 'revenue', 'classes_count',
            'bookings_count', 'review_count', 'subcategories_list',
            'classFormats_list', 'skillLevels_list', 'ageGroups_list',
        ]
        
    def get_subcategories_list(self, obj):
        return obj.subcategories if isinstance(obj.subcategories, list) else []

    def get_classFormats_list(self, obj):
        return obj.classFormats if isinstance(obj.classFormats, list) else []

    def get_skillLevels_list(self, obj):
        return obj.skillLevels if isinstance(obj.skillLevels, list) else []

    def get_ageGroups_list(self, obj):
        return obj.ageGroups if isinstance(obj.ageGroups, list) else []

    def get_managers_emails(self, obj):
        if hasattr(obj, 'managers'):
            return [manager.email for manager in obj.managers.all()]
        return []
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.classCategory:
            representation['classCategory'] = instance.classCategory.key
        return representation