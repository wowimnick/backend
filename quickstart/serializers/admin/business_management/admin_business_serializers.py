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
            'businessId',         # Read-only PK
            'businessName',       # Editable
            'businessType',       # Editable
            'businessImage',    # Editable (handles file upload/URL)
            'businessDescription',# Editable
            'createdAt',          # Read-only

            # --- Operational Details ---
            'openingTime',        # Editable
            'closingTime',        # Editable
            'cancellationPolicy', # Editable
            'liabilityWaiver',    # Editable

            # --- Contact Info ---
            'studentContactPhone',# Editable
            'studentContactEmail',# Editable
            'preferredContact',   # Editable

            # --- Location ---
            'businessAddress',    # Editable
            'businessCity',       # Editable
            'businessState',      # Editable
            'businessZipCode',    # Editable
            'latitude',           # Editable
            'longitude',          # Editable
            'showExactLocation',  # Editable

            # --- Classification (Editable Strings) ---
            'classCategory',      # Editable
            'subcategories',      # Editable (Admin updates the comma-separated string)
            'classFormats',       # Editable
            'skillLevels',        # Editable
            'ageGroups',          # Editable

            # --- Admin Controls & Status ---
            'featured',           # Editable (Admin can feature/unfeature)
            'isActive',           # Editable (Admin can activate/deactivate)
            'verificationStatus', # Read-only (Managed by verification process)

            # --- Related Info (Read-Only) ---
            'owner_email',        # Read-only
            'managers_emails',    # Read-only Method Field

            # --- Annotated Metrics (Read-Only) ---
            'status',  # Read-only
            'rating',             # Read-only
            'revenue',            # Read-only
            'classes_count',      # Read-only
            'bookings_count',     # Read-only
            'review_count',       # Read-only

            # --- List Representations (Read-Only) ---
            'subcategories_list', # Read-only Method Field
            'classFormats_list',  # Read-only Method Field
            'skillLevels_list',   # Read-only Method Field
            'ageGroups_list',     # Read-only Method Field
        ]

        # Explicitly list fields that CANNOT be updated via this serializer
        read_only_fields = [
            'businessId',         # Cannot change PK
            'createdAt',          # Cannot change creation time
            'verificationStatus', # Managed elsewhere
            'owner_email',        # Display only
            'managers_emails',    # Display only (Method Field)
            # Annotated fields are derived, not set directly
            'status',
            'rating',
            'revenue',
            'classes_count',
            'bookings_count',
            'review_count',
            # List representations are derived, not set directly
            'subcategories_list',
            'classFormats_list',
            'skillLevels_list',
            'ageGroups_list',
        ]
        # NOTE: Any field listed in `fields` but NOT in `read_only_fields` is considered writable/editable.
        # This includes 'businessName', 'businessType', 'businessDescription', 'openingTime', etc.
        # It also includes the comma-separated string fields like 'subcategories', allowing admins to edit the source string.

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
