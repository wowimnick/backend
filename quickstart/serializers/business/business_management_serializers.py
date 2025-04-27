from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework.exceptions import ValidationError as DRFValidationError
from decimal import Decimal
import json # Import json for parsing
import logging

# Adjust import paths as needed
from ...models import BusinessInfo, ClassesMain, Reviews, CustomUser, Booking, VerificationRequest
from django.db.models import Sum, Count, Avg, Q, Subquery, OuterRef, IntegerField, F
from django.db.models.functions import Coalesce
from datetime import timedelta
from django.utils import timezone

logger = logging.getLogger(__name__)

# Define colors if not imported from elsewhere
colors = {
    'chart': {
        'blue': '#3b82f6',
        'green': '#10b981',
        'purple': '#8b5cf6',
        'orange': '#f97316',
        'red': '#ef4444'
    }
}

class MetricSerializer(serializers.Serializer):
    value = serializers.FloatField() # Or IntegerField/DecimalField as appropriate
    change = serializers.FloatField() # Percentage or absolute change

class RevenueTrendItemSerializer(serializers.Serializer):
    date = serializers.CharField() # ISO date string (YYYY-MM-DD or YYYY-MM)
    revenue = serializers.FloatField()

class UpcomingClassSerializer(serializers.Serializer):
    name = serializers.CharField()
    time = serializers.CharField()
    current_occupancy = serializers.IntegerField()
    max_occupancy = serializers.IntegerField()

class PopularClassSerializer(serializers.Serializer):
    name = serializers.CharField()
    enrollment = serializers.IntegerField()

class RecentActivitySerializer(serializers.Serializer):
    message = serializers.CharField()
    time = serializers.CharField() # Formatted time string
    icon = serializers.CharField() # Name of the Lucide icon
    color = serializers.CharField()

class MetricsContainerSerializer(serializers.Serializer):
    total_students = MetricSerializer()
    active_classes = MetricSerializer()
    monthly_revenue = MetricSerializer()
    average_rating = MetricSerializer()

class BusinessDashboardOverviewSerializer(serializers.Serializer):
    """Serializer for the aggregated business overview dashboard"""
    metrics = MetricsContainerSerializer()
    revenue_trend = RevenueTrendItemSerializer(many=True)
    upcoming_classes = UpcomingClassSerializer(many=True)
    popular_classes = PopularClassSerializer(many=True)
    recent_activity = RecentActivitySerializer(many=True)

class BusinessRegistrationSerializer(serializers.ModelSerializer):
    """
    Handles validating and creating BusinessInfo during registration.
    Expects list fields (subcategories, etc.) as JSON strings from FormData.
    REMOVED verificationDocument and related validations.
    ADDED validation for termsAccepted and privacyAccepted.
    """
    latitude = serializers.DecimalField(max_digits=10, decimal_places=8, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=11, decimal_places=8, required=False, allow_null=True)

    # Expect JSON strings for list fields from FormData
    subcategories = serializers.CharField(write_only=True, required=False, allow_blank=True)
    classFormats = serializers.CharField(write_only=True, required=False, allow_blank=True)
    skillLevels = serializers.CharField(write_only=True, required=False, allow_blank=True)
    ageGroups = serializers.CharField(write_only=True, required=False, allow_blank=True)

    # verificationDocument removed from here

    class Meta:
        model = BusinessInfo
        fields = [
            # Step 0: Business Info
            'businessName', 'businessType', 'businessDescription',
            'businessImage', 'openingTime', 'closingTime',
            'liabilityWaiver',

            # Step 1: Contact Details
            'studentContactPhone', 'studentContactEmail',
            'preferredContact',

            # Step 2: Location
            'businessAddress', 'businessCity',
            'businessState', 'businessZipCode', 'latitude', 'longitude',
            'showExactLocation', # Controlled by latitude/longitude presence maybe?

            # Step 3: Class Types & Agreements
            'classCategory',
            'subcategories', 'classFormats', 'skillLevels', 'ageGroups', # JSON strings
            'termsAccepted', 'privacyAccepted' # Moved from Verification
        ]
        extra_kwargs = {
            # Step 0 fields
            'businessName': {'required': True}, 'businessType': {'required': True},
            'businessDescription': {'required': True}, 'openingTime': {'required': True},
            'closingTime': {'required': True}, 
            'liabilityWaiver': {'required': True}, # Make sure frontend sends this
            'businessImage': {'required': False, 'allow_null': True},

            # Step 1 fields
            'studentContactPhone': {'required': True}, 'studentContactEmail': {'required': True},
            'preferredContact': {'required': True},

            # Step 2 fields
            'businessAddress': {'required': True}, 'businessCity': {'required': True},
            'businessState': {'required': True}, 'businessZipCode': {'required': True},

            # Step 3 fields
            'classCategory': {'required': True},
            # List fields are CharField here, required depends on if the frontend *always* sends them (even empty string/JSON)
            'subcategories': {'required': False, 'allow_blank': True}, # Make optional if empty list is acceptable
            'classFormats': {'required': False, 'allow_blank': True},
            'skillLevels': {'required': False, 'allow_blank': True},
            'ageGroups': {'required': False, 'allow_blank': True},
            'termsAccepted': {'required': True}, # Agreements are mandatory
            'privacyAccepted': {'required': True},

            # Fields not directly set by user during registration
            'showExactLocation': {'read_only': True}, # Determined by lat/lon
        }

    def validate(self, data):
        # --- Email Validation ---
        for email_field in ['studentContactEmail']: # Removed adminContactEmail
            if email_field in data and data[email_field]: # Check if field exists and is not empty
                try:
                    validate_email(data[email_field])
                except DjangoValidationError:
                    raise DRFValidationError({email_field: "Invalid email address"})

        # --- Time Validation ---
        if data.get('openingTime') and data.get('closingTime'):
            if data['openingTime'] >= data['closingTime']:
                raise DRFValidationError({"closingTime": "Closing time must be after opening time"})

        # --- AGREEMENTS Validation ---
        if not data.get('termsAccepted'):
            raise DRFValidationError({"termsAccepted": "You must accept the Terms of Service"})
        if not data.get('privacyAccepted'):
            raise DRFValidationError({"privacyAccepted": "You must accept the Privacy Policy"})

        # --- Liability Waiver Validation ---
        # Ensure it's treated as a boolean
        liability_waiver = data.get('liabilityWaiver')
        if isinstance(liability_waiver, str):
             data['liabilityWaiver'] = liability_waiver.lower() == 'true'
        elif liability_waiver is None: # Or handle if it's not required / missing
             raise DRFValidationError({"liabilityWaiver": "Liability waiver agreement is required."})
        elif not isinstance(liability_waiver, bool):
             raise DRFValidationError({"liabilityWaiver": "Invalid value for liability waiver."})

        if not data.get('liabilityWaiver'): # Check the boolean value now
             raise DRFValidationError({"liabilityWaiver": "You must agree to the liability waiver."})


        # --- Coordinate Validation ---
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        if (latitude is not None and longitude is None) or \
           (longitude is not None and latitude is None):
             raise DRFValidationError("Both latitude and longitude must be provided together, or neither.")
        # Set showExactLocation based on coordinate presence
        data['showExactLocation'] = (latitude is not None and longitude is not None)

        # --- List Field JSON Parsing ---
        list_fields = ['subcategories', 'classFormats', 'skillLevels', 'ageGroups']
        for field in list_fields:
            field_value = data.get(field)
            if field_value: # Check if field exists and is not empty string/None
                try:
                    # Parse the JSON string into a Python list
                    parsed_list = json.loads(field_value)
                    if not isinstance(parsed_list, list):
                        raise DRFValidationError({field: "Invalid format. Expected a list."})
                    # Replace the JSON string with the parsed list in validated_data
                    data[field] = parsed_list
                except json.JSONDecodeError:
                    raise DRFValidationError({field: f"Invalid JSON format provided for {field}."})
                except TypeError:
                    # Handle cases where field_value might already be a list (e.g., if not sent via FormData)
                    if not isinstance(field_value, list):
                         raise DRFValidationError({field: f"Unexpected type for {field}. Expected JSON string or list."})
                    # If it's already a list, keep it as is
                    data[field] = field_value

            elif field in data: # Field exists but is empty string or None
                 # Handle empty string/None case - convert to empty list for JSONField
                 data[field] = []
            # If field not present at all in data, model's default=list will handle it

        data['isActive'] = False # Default to inactive until verified

        return data

    def create(self, validated_data):
        user = self.context['request'].user

        # Ensure boolean fields are correctly interpreted
        validated_data['termsAccepted'] = validated_data.get('termsAccepted', False)
        validated_data['privacyAccepted'] = validated_data.get('privacyAccepted', False)
        validated_data['liabilityWaiver'] = validated_data.get('liabilityWaiver', False)

        # Pop 'isActive' if it's in validated_data from validate method,
        # as we'll set it explicitly later based on verification
        validated_data.pop('isActive', None)

        # Create the BusinessInfo instance
        business = BusinessInfo.objects.create(
            owner=user,
            verificationStatus='pending', # Default status
            isActive=False, # Explicitly set to inactive on creation
            **validated_data # Pass all validated data
        )

        VerificationRequest.objects.create(
            user=user,
            business=business,
            status='pending'
        )

        return business

class ManagedBusinessInfoSerializer(serializers.ModelSerializer):
    """
    Serializer for Business Owners/Managers viewing/editing their OWN profile.
    Reads from JSONFields for list data.
    """
    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    managers_emails = serializers.SerializerMethodField(read_only=True)

    # Mapped fields for frontend convenience
    email = serializers.EmailField(source='studentContactEmail', required=False)
    phone = serializers.CharField(source='studentContactPhone', required=False)
    location = serializers.CharField(source='businessAddress', required=False)
    # saltLocation is handled in validate based on latitude/longitude

    # Direct model fields relevant to settings UI
    openingTime = serializers.TimeField(format='%H:%M', required=False, allow_null=True)
    closingTime = serializers.TimeField(format='%H:%M', required=False, allow_null=True)
    latitude = serializers.DecimalField(max_digits=10, decimal_places=8, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=11, decimal_places=8, required=False, allow_null=True)
    businessImage = serializers.ImageField(required=False, allow_null=True, use_url=True)

    # List fields (read-only representation of JSONField)
    subcategories = serializers.ListField(child=serializers.CharField(), read_only=True)
    classFormats = serializers.ListField(child=serializers.CharField(), read_only=True)
    skillLevels = serializers.ListField(child=serializers.CharField(), read_only=True)
    ageGroups = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId',
            'businessName',
            'businessType',
            'businessDescription',
            'businessImage', # For display (URL) and update (File)
            'website',

            # General Contact (mapped)
            'email', # source='studentContactEmail'
            'phone', # source='studentContactPhone'

            # Location
            'location', # source='businessAddress'
            'businessCity', # Direct field
            'businessState', # Direct field
            'businessZipCode', # Direct field
            'latitude',
            'longitude',
            'showExactLocation', # Read-only display

            # Simplified Booking/Policies
            'refundPolicy',

            # Preferences/Settings
            'openingTime',
            'closingTime',
            'preferredContact', # Direct field
            'newBookingNotification',
            'cancellationNotification',
            'reminderNotification',
            'smsNotifications',
            'liabilityWaiver', # Direct field

            # Category Info (read-only)
            'classCategory',
            'subcategories',
            'classFormats',
            'skillLevels',
            'ageGroups',

            # Read-only context fields
            'owner_email',
            'managers_emails',
            'verificationStatus',
            'stripe_account_id',
            'stripe_account_status',
            'createdAt',
            'updatedAt',
        ]
        read_only_fields = (
            'businessId',
            'owner_email',
            'managers_emails',
            'verificationStatus',
            'stripe_account_id',
            'stripe_account_status',
            'createdAt',
            'updatedAt',
            'showExactLocation',
            # List fields are read-only representations
            'subcategories',
            'classFormats',
            'skillLevels',
            'ageGroups',
        )
        extra_kwargs = {
             # Make fields optional for PATCH updates
             'businessName': {'required': False},
             'businessType': {'required': False},
             'businessDescription': {'required': False},
             'openingTime': {'required': False, 'allow_null': True}, # Allow nulling time
             'closingTime': {'required': False, 'allow_null': True},
             'refundPolicy': {'required': False},
             'studentContactEmail': {'required': False}, # Allow updating via 'email' alias
             'studentContactPhone': {'required': False}, # Allow updating via 'phone' alias
             'businessAddress': {'required': False}, # Allow updating via 'location' alias
             'businessCity': {'required': False},
             'businessState': {'required': False},
             'businessZipCode': {'required': False},
             'latitude': {'required': False, 'allow_null': True},
             'longitude': {'required': False, 'allow_null': True},
             'preferredContact': {'required': False},
             'classCategory': {'required': False},
             'liabilityWaiver': {'required': False},
        }

    def get_managers_emails(self, obj):
        if hasattr(obj, 'managers'):
             # Ensure managers are prefetched for efficiency
             return [manager.email for manager in obj.managers.all()]
        return []

    # Removed get_subcategories_list etc, as JSONField is returned directly

    def validate_email(self, value):
        # This validates the 'email' alias field if provided
        if value: # Only validate if not empty
            try:
                validate_email(value)
            except DjangoValidationError:
                raise DRFValidationError("Invalid email address provided.")
        return value

    def validate(self, data):
        # Use instance values if fields are not provided in partial update
        instance = getattr(self, 'instance', None)

        opening = data.get('openingTime', instance.openingTime if instance else None)
        closing = data.get('closingTime', instance.closingTime if instance else None)
        if opening and closing and opening >= closing:
            raise DRFValidationError({"closingTime": "Closing time must be after opening time."})

        # Handle showExactLocation based on coordinates
        latitude = data.get('latitude', instance.latitude if instance else None)
        longitude = data.get('longitude', instance.longitude if instance else None)
        if (latitude is not None and longitude is None) or \
           (longitude is not None and latitude is None):
             raise DRFValidationError("Both latitude and longitude must be provided together, or neither.")
        # Update showExactLocation based on the final state of coordinates
        data['showExactLocation'] = (latitude is not None and longitude is not None)

        # Handle image clearing
        # If frontend sends businessImage: null or businessImage: '', treat as request to clear
        business_image_input = self.context['request'].data.get('businessImage', ...) # Use ... as sentinel
        if business_image_input is None or business_image_input == '':
            data['businessImage'] = None # Explicitly set to None for update logic

        return data

    def update(self, instance, validated_data):
        # Handle image deletion/update
        new_image = validated_data.get('businessImage', ...) # Use sentinel again

        if new_image is None: # Explicit request to clear
            if instance.businessImage:
                instance.businessImage.delete(save=False) # Delete file from storage
            instance.businessImage = None # Clear the field in the model
        elif new_image is not ...: # A new file was uploaded
            # If there's an existing image different from the new one, delete the old file
            if instance.businessImage and instance.businessImage.name != new_image.name:
                 instance.businessImage.delete(save=False)
            instance.businessImage = new_image # Assign the new file object
        # If new_image is ..., it means the businessImage field was not sent in the request,
        # so we don't touch the existing image.

        # Remove the potentially processed 'businessImage' key if it was None or a File object,
        # so super().update doesn't try to handle it again if it wasn't a direct model field update
        validated_data.pop('businessImage', None)

        # Map aliases back to model fields for update
        if 'email' in validated_data:
            instance.studentContactEmail = validated_data.pop('email')
        if 'phone' in validated_data:
            instance.studentContactPhone = validated_data.pop('phone')
        if 'location' in validated_data:
            instance.businessAddress = validated_data.pop('location')

        # Update remaining fields
        return super().update(instance, validated_data)

class BusinessStatsSerializer(serializers.ModelSerializer):
    """Serializer for dashboard stats - NO CHANGES NEEDED HERE"""
    total_revenue = serializers.SerializerMethodField()
    total_students = serializers.SerializerMethodField()
    total_classes = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    recent_bookings = serializers.SerializerMethodField()
    registration_date = serializers.DateTimeField(source='createdAt', read_only=True)
    totalReviews = serializers.SerializerMethodField() # Added this method

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'businessImage',
            'totalReviews',
            'total_revenue', 'total_students', 'total_classes',
            'average_rating', 'recent_bookings',
            'registration_date'
        ]

    def get_totalReviews(self, obj):
        # Add method to calculate total reviews
        return obj.reviews_set.filter(status='approved').count()

    def get_average_rating(self, obj):
        avg = obj.reviews_set.filter(status='approved').aggregate(avg=Avg('rating'))['avg'] or 0.0
        return round(avg, 1)

    def get_total_revenue(self, obj):
        # Calculate based on valid bookings (first booking per course group)
        valid_booking_ids = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=obj,
            payment_status='paid'
        ).filter(
            Q(booking_group_id__isnull=True) |
            Q(id=Subquery(
                Booking.objects.filter(booking_group_id=OuterRef('booking_group_id'))
                .order_by('id').values('id')[:1]
            ))
        ).values_list('id', flat=True)

        total = Booking.objects.filter(id__in=list(valid_booking_ids)).aggregate(
            total=Sum('amount_paid')
        )['total'] or Decimal('0.00')
        return float(total)


    def get_total_students(self, obj):
        # Count distinct users associated with *any* booking for this business
        return Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=obj
        ).values('user').distinct().count()

    def get_total_classes(self, obj):
        return obj.classesmain_set.filter(status='active').count()

    def get_recent_bookings(self, obj):
        thirty_days_ago = timezone.now() - timedelta(days=30)
        # Count valid bookings in the last 30 days
        return Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=obj,
            booking_date__gte=thirty_days_ago,
            payment_status='paid' # Count only paid bookings here too? Or all attempts? Let's count paid.
        ).filter(
            Q(booking_group_id__isnull=True) |
            Q(id=Subquery(
                Booking.objects.filter(booking_group_id=OuterRef('booking_group_id'))
                .order_by('id').values('id')[:1]
            ))
        ).count()