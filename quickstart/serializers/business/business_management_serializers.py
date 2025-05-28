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
from datetime import timedelta, time
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
    date = serializers.CharField() # ISO date string (YYYY-MM-DD) - this is now local business date
    gross_revenue = serializers.FloatField(required=False, default=0.0) 
    platform_fees = serializers.FloatField(required=False, default=0.0)
    net_revenue = serializers.FloatField(required=False, default=0.0)

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
    icon = serializers.CharField()    # Name of the Lucide icon
    color = serializers.CharField()
    timestamp = serializers.DateTimeField(read_only=True)
class MetricsContainerSerializer(serializers.Serializer):
    total_students = MetricSerializer()
    active_classes = MetricSerializer()
    monthly_revenue = MetricSerializer()
    average_rating = MetricSerializer()

class SetupProgressSerializer(serializers.Serializer):
    is_stripe_connected = serializers.BooleanField()
    is_profile_complete = serializers.BooleanField()
    has_created_class = serializers.BooleanField()
    has_class_options = serializers.BooleanField()
    has_schedules = serializers.BooleanField()

class BusinessDashboardOverviewSerializer(serializers.Serializer):
    """Serializer for the aggregated business overview dashboard"""
    metrics = MetricsContainerSerializer()
    revenue_trend = RevenueTrendItemSerializer(many=True)
    upcoming_classes = UpcomingClassSerializer(many=True)
    popular_classes = PopularClassSerializer(many=True)
    recent_activity = RecentActivitySerializer(many=True)
    today_snapshot = serializers.DictField(child=serializers.IntegerField(), required=False) 
    actionable_prompts = serializers.DictField(required=False) 
    setup_progress = SetupProgressSerializer(required=False) 

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
    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    managers_emails = serializers.SerializerMethodField(read_only=True)

    # Fields from BusinessSettings.jsx (General Tab)
    # businessName, businessType, businessDescription are direct model fields
    studentContactEmail = serializers.EmailField(required=False, allow_blank=True) # Primary email
    studentContactPhone = serializers.CharField(required=False, allow_blank=True) # Primary phone
    # website is a direct model field

    # Location Tab
    # businessAddress, latitude, longitude, showExactLocation are direct model fields
    
    # Preferences Tab
    # openingTime, closingTime are direct model fields (TimeField)
    business_timezone = serializers.CharField(required=False, allow_blank=True) # Maps to model's business_timezone
    # newBookingNotification, cancellationNotification, reminderNotification, smsNotifications are direct model fields (BooleanField)

    # Business Image (handled specially in update)
    businessImage = serializers.ImageField(required=False, allow_null=True, use_url=True)
    
    # Read-only representations of JSONFields (if still needed for display, but edit forms won't use these directly)
    subcategories = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)
    classFormats = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)
    skillLevels = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)
    ageGroups = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)


    class Meta:
        model = BusinessInfo
        # List all fields that can be read or written via this serializer
        # Ensure this matches the fields you intend to manage via BusinessSettings.jsx
        fields = [
            'businessId', 'businessName', 'businessType', 'businessDescription',
            'businessImage', 'website',
            'studentContactEmail', 'studentContactPhone', # Primary contact fields
            'businessAddress', 'businessCity', 'businessState', 'businessZipCode', # If still used, otherwise rely on full address and parse
            'latitude', 'longitude', 'showExactLocation',
            'openingTime', 'closingTime', 'business_timezone', # Make sure model field is 'business_timezone'
            'preferredContact', # If still used from General tab for example
            'newBookingNotification', 'cancellationNotification', 'reminderNotification', 'smsNotifications',
            'liabilityWaiver', # If editable in settings
            'classCategory', # Read-only representation
            'subcategories','classFormats', 'skillLevels', 'ageGroups', # Read-only JSON representations
            'owner_email', 'managers_emails', 'verificationStatus',
            'stripe_account_id', 'stripe_account_status',
            'createdAt', 'updatedAt',
        ]
        read_only_fields = ( # Fields not settable by the user via this settings form
            'businessId', 'owner_email', 'managers_emails', 'verificationStatus',
            'stripe_account_id', 'stripe_account_status',
            'createdAt', 'updatedAt',
            'classCategory', 
            'subcategories', 'classFormats', 'skillLevels', 'ageGroups' # Read-only views of JSON
        )
        extra_kwargs = {
             # Make fields optional for PATCH updates
             'businessName': {'required': False}, 'businessType': {'required': False},
             'businessDescription': {'required': False},
             'studentContactEmail': {'required': False, 'allow_blank': True},
             'studentContactPhone': {'required': False, 'allow_blank': True},
             'website': {'required': False, 'allow_blank': True},
             'businessAddress': {'required': False, 'allow_blank': True},
             'latitude': {'required': False, 'allow_null': True},
             'longitude': {'required': False, 'allow_null': True},
             'openingTime': {'required': False, 'allow_null': True},
             'closingTime': {'required': False, 'allow_null': True},
             'business_timezone': {'required': False, 'allow_blank': True},
             'preferredContact': {'required': False},
             'liabilityWaiver': {'required': False}, # Assuming this can be toggled
        }

    def get_managers_emails(self, obj):
        if hasattr(obj, 'managers'):
             return [manager.email for manager in obj.managers.all()]
        return []

    def validate_studentContactEmail(self, value):
        if value:
            try: validate_email(value)
            except DjangoValidationError: raise DRFValidationError("Invalid business email address.")
        return value
    
    def validate_openingTime(self, value):
        if isinstance(value, str): # Frontend might send HH:mm string
            try: return time.fromisoformat(value)
            except ValueError: raise DRFValidationError("Invalid opening time format. Use HH:MM.")
        return value # Assume it's already a time object

    def validate_closingTime(self, value):
        if isinstance(value, str):
            try: return time.fromisoformat(value)
            except ValueError: raise DRFValidationError("Invalid closing time format. Use HH:MM.")
        return value

    def validate(self, data):
        instance = getattr(self, 'instance', None)
        opening = data.get('openingTime', instance.openingTime if instance else None)
        closing = data.get('closingTime', instance.closingTime if instance else None)
        if opening and closing and opening >= closing:
            raise DRFValidationError({"closingTime": "Closing time must be after opening time."})

        latitude = data.get('latitude', instance.latitude if instance else None)
        longitude = data.get('longitude', instance.longitude if instance else None)
        if (latitude is not None and longitude is None) or \
           (longitude is not None and latitude is None):
             raise DRFValidationError("Both latitude and longitude must be provided, or neither.")
        # Set showExactLocation based on coordinate presence for the update
        # The actual model field `showExactLocation` will be updated if `saltLocation` was sent from frontend
        # or based on lat/lon. Backend model should handle the `showExactLocation` logic based on `saltLocation`
        # sent from frontend, or derive from lat/lon presence if `saltLocation` isn't explicitly sent.
        # For settings, frontend controls 'hideExactLocation' which maps to 'saltLocation'
        # Let's assume frontend sends 'showExactLocation' directly if that's the model field,
        # or if it sends 'saltLocation', the view/serializer maps it.
        # The frontend `BusinessSettings.jsx` sends `showExactLocation = !values.saltLocation`
        # So, if `showExactLocation` is in `data`, use it. Otherwise, derive.
        if 'showExactLocation' not in data: # If frontend didn't send it (e.g. location tab not saved)
            data['showExactLocation'] = (latitude is not None and longitude is not None)
        
        business_image_input = self.context['request'].data.get('businessImage', ...)
        if business_image_input == '': # Explicit empty string means remove
            data['businessImage'] = None
        elif business_image_input is ...: # Not provided in request, remove from data to keep existing
            data.pop('businessImage', None)
        # If it's a File object, it will be handled by DRF default.

        return data

    def update(self, instance, validated_data):
        # Handle businessImage: None means clear, file object means update/new
        # If 'businessImage' is not in validated_data, it means no change was requested for it.
        
        new_image_file = validated_data.pop('businessImage', ...) # Use sentinel if not present

        if new_image_file is None: # Explicitly set to None in validate if frontend sent empty string
            if instance.businessImage:
                try: instance.businessImage.delete(save=False)
                except Exception as e: logger.warning(f"Error deleting old businessImage for {instance.pk}: {e}")
            instance.businessImage = None
        elif new_image_file is not ...: # A new file object was provided
            if instance.businessImage: # Delete old if exists
                 try: instance.businessImage.delete(save=False)
                 except Exception as e: logger.warning(f"Error deleting old businessImage for {instance.pk} before update: {e}")
            instance.businessImage = new_image_file
        # If new_image_file is ..., field wasn't in validated_data, so no change to instance.businessImage

        # Ensure direct model fields are updated
        # Fields like 'studentContactEmail' are now direct model fields in `validated_data`
        # No need for manual alias mapping if frontend sends correct field names.
        
        # Update showExactLocation based on coordinates if not explicitly set by a saltLocation toggle
        # The validate method should have already set 'showExactLocation' in validated_data
        # if latitude/longitude were part of the update.
        if 'latitude' in validated_data and 'longitude' in validated_data:
            instance.latitude = validated_data.get('latitude', instance.latitude)
            instance.longitude = validated_data.get('longitude', instance.longitude)
            # Re-derive showExactLocation based on final coordinates, unless saltLocation was explicitly sent
            # The `BusinessInfo` model's `save` method could also handle this derivation.
            # For now, assuming frontend sends `showExactLocation` if it was toggled.
            if 'showExactLocation' in validated_data: # If frontend sent it (from saltLocation toggle)
                 instance.showExactLocation = validated_data.get('showExactLocation')
            else: # Derive if not explicitly sent
                 instance.showExactLocation = (instance.latitude is not None and instance.longitude is not None)


        # Let super().update handle the rest of the fields
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