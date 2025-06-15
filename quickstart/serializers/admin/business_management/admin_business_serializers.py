# --- START OF FILE admin_business_serializers.py ---

from rest_framework import serializers
from ....models import BusinessInfo
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
    Includes read-only list representations for comma-separated fields.
    """
    # --- Related Fields (Read-Only) ---
    owner_email = serializers.EmailField(source='owner.email', read_only=True, allow_null=True)
    managers_emails = serializers.SerializerMethodField(read_only=True) # Display only

    # --- Annotated Fields (Read-Only - Expected from ViewSet's get_queryset) ---
    status = serializers.CharField(read_only=True, default='unknown')
    rating = serializers.FloatField(read_only=True, default=0.0)
    revenue = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True, default=Decimal('0.00'))
    classes_count = serializers.IntegerField(read_only=True, default=0)
    bookings_count = serializers.IntegerField(read_only=True, default=0)
    review_count = serializers.IntegerField(read_only=True, default=0)

    # --- List Representations of Comma-Separated Fields (Read-Only) ---
    subcategories_list = serializers.SerializerMethodField(read_only=True)
    classFormats_list = serializers.SerializerMethodField(read_only=True)
    skillLevels_list = serializers.SerializerMethodField(read_only=True)
    ageGroups_list = serializers.SerializerMethodField(read_only=True)

    # --- Model Fields ---
    # businessImage is handled by ModelSerializer, allows update if not read_only
    # Other model fields are included below

    class Meta:
        model = BusinessInfo
        # Define ALL fields this serializer should handle (display or update)
        fields = [
            # --- Identifiers & Core Info ---
            'businessId',
            'businessName',
            'businessType',
            'businessImage',
            'businessDescription',
            'createdAt',

            # --- Operational Details ---
            'openingTime',
            'closingTime',
            'liabilityWaiver',

            # --- Contact Info ---
            'studentContactPhone',
            'studentContactEmail',
            'preferredContact',
            'website', # ADDED

            # --- Location ---
            'businessAddress',
            'businessCity',
            'businessState',
            'businessZipCode',
            'latitude',
            'longitude',
            'showExactLocation',

            # --- Classification (Editable Strings) ---
            'classCategory',
            'subcategories',
            'classFormats',
            'skillLevels',
            'ageGroups',
            
            # --- NEW: Social Media & SEO ---
            'social_media_links',
            'tags_keywords',
            # --- END NEW ---

            # --- Admin Controls & Status ---
            'featured',
            'isActive',
            'verificationStatus',

            # --- Related Info (Read-Only) ---
            'owner_email',
            'managers_emails',

            # --- Annotated Metrics (Read-Only) ---
            'status',
            'rating',
            'revenue',
            'classes_count',
            'bookings_count',
            'review_count',

            # --- List Representations (Read-Only) ---
            'subcategories_list',
            'classFormats_list',
            'skillLevels_list',
            'ageGroups_list',
        ]

        # Explicitly list fields that CANNOT be updated via this serializer
        read_only_fields = [
            'businessId',
            'createdAt',
            'verificationStatus',
            'owner_email',
            'managers_emails',
            'status',
            'rating',
            'revenue',
            'classes_count',
            'bookings_count',
            'review_count',
            'subcategories_list',
            'classFormats_list',
            'skillLevels_list',
            'ageGroups_list',
        ]
        
    # --- Methods for Read-Only List Representations ---
    def get_subcategories_list(self, obj):
        return _split_string_to_list(obj.subcategories)

    def get_classFormats_list(self, obj):
        return _split_string_to_list(obj.classFormats)

    def get_skillLevels_list(self, obj):
        return _split_string_to_list(obj.skillLevels)

    def get_ageGroups_list(self, obj):
        return _split_string_to_list(obj.ageGroups)

    def get_managers_emails(self, obj):
        # Access managers through the related name
        if hasattr(obj, 'managers'): # Check if the M2M field exists
            return [manager.email for manager in obj.managers.all()]
        return []

    # No custom 'update' needed if frontend sends comma-separated strings
    # for subcategories, classFormats, etc. during updates. The ModelSerializer
    # will handle saving these string fields directly.
