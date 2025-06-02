from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email, URLValidator
from rest_framework.exceptions import ValidationError as DRFValidationError
from decimal import Decimal
import json
import logging
import pytz # For timezone choices

from ...models import BusinessInfo, ClassesMain, Reviews, CustomUser, Booking, VerificationRequest
from django.db.models import Sum, Count, Avg, Q, Subquery, OuterRef, IntegerField, F
from django.db.models.functions import Coalesce
from datetime import timedelta, time, datetime # Added datetime for founding_year validation
from django.utils import timezone

logger = logging.getLogger(__name__)

colors = {
    'chart': {
        'blue': '#3b82f6',
        'green': '#10b981',
        'purple': '#8b5cf6',
        'orange': '#f97316',
        'red': '#ef4444'
    }
}

COMMON_TIMEZONE_CHOICES_SERIALIZER = [(tz, tz.replace("_", " ")) for tz in pytz.common_timezones]

# ... (Keep MetricSerializer, RevenueTrendItemSerializer, etc. as they are) ...
class MetricSerializer(serializers.Serializer):
    value = serializers.FloatField()
    change = serializers.FloatField()

class RevenueTrendItemSerializer(serializers.Serializer):
    date = serializers.CharField()
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
    icon = serializers.CharField()
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
    metrics = MetricsContainerSerializer()
    revenue_trend = RevenueTrendItemSerializer(many=True)
    upcoming_classes = UpcomingClassSerializer(many=True)
    popular_classes = PopularClassSerializer(many=True)
    recent_activity = RecentActivitySerializer(many=True)
    today_snapshot = serializers.DictField(child=serializers.IntegerField(), required=False)
    actionable_prompts = serializers.DictField(required=False)
    setup_progress = SetupProgressSerializer(required=False)


class BusinessRegistrationSerializer(serializers.ModelSerializer):
    latitude = serializers.DecimalField(max_digits=10, decimal_places=8, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=11, decimal_places=8, required=False, allow_null=True)

    subcategories = serializers.CharField(write_only=True, required=False, allow_blank=True)
    classFormats = serializers.CharField(write_only=True, required=False, allow_blank=True)
    skillLevels = serializers.CharField(write_only=True, required=False, allow_blank=True)
    ageGroups = serializers.CharField(write_only=True, required=False, allow_blank=True)
    
    # New fields for registration
    website = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    business_timezone = serializers.ChoiceField(choices=COMMON_TIMEZONE_CHOICES_SERIALIZER, required=True)
    social_media_links = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="JSON string of social media links, e.g. {'facebook':'url'}")
    tags_keywords = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="JSON string of a list of keywords")
    founding_year = serializers.IntegerField(required=False, allow_null=True)


    class Meta:
        model = BusinessInfo
        fields = [
            # Step 0: Business Info
            'businessName', 'businessType', 'businessDescription',
            'businessImage', 'openingTime', 'closingTime',
            'liabilityWaiver',
            'website', # ADDED
            'business_timezone', # ADDED
            'social_media_links', # ADDED (as CharField for JSON string)
            'tags_keywords', # ADDED (as CharField for JSON string)
            'founding_year', # ADDED

            # Step 1: Contact Details
            'studentContactPhone', 'studentContactEmail',
            'preferredContact',

            # Step 2: Location
            'businessAddress', 'businessCity',
            'businessState', 'businessZipCode', 'latitude', 'longitude',
            'showExactLocation',

            # Step 3: Class Types & Agreements
            'classCategory',
            'subcategories', 'classFormats', 'skillLevels', 'ageGroups',
            'termsAccepted', 'privacyAccepted'
        ]
        extra_kwargs = {
            # Step 0 fields
            'businessName': {'required': True}, 'businessType': {'required': True},
            'businessDescription': {'required': True}, 'openingTime': {'required': True},
            'closingTime': {'required': True},
            'liabilityWaiver': {'required': True},
            'businessImage': {'required': False, 'allow_null': True},
            'website': {'required': False, 'allow_blank': True, 'allow_null': True},
            'business_timezone': {'required': True},
            'social_media_links': {'required': False, 'allow_blank': True},
            'tags_keywords': {'required': False, 'allow_blank': True},
            'founding_year': {'required': False, 'allow_null': True},


            # Step 1 fields
            'studentContactPhone': {'required': True}, 'studentContactEmail': {'required': True},
            'preferredContact': {'required': True},

            # Step 2 fields
            'businessAddress': {'required': True}, 'businessCity': {'required': True},
            'businessState': {'required': True}, 'businessZipCode': {'required': True},

            # Step 3 fields
            'classCategory': {'required': True},
            'subcategories': {'required': False, 'allow_blank': True},
            'classFormats': {'required': False, 'allow_blank': True},
            'skillLevels': {'required': False, 'allow_blank': True},
            'ageGroups': {'required': False, 'allow_blank': True},
            'termsAccepted': {'required': True},
            'privacyAccepted': {'required': True},

            'showExactLocation': {'read_only': True},
        }

    def validate_website(self, value):
        if value: # Only validate if a value is provided
            validator = URLValidator()
            try:
                validator(value)
            except DjangoValidationError:
                raise DRFValidationError("Invalid URL format for website.")
        return value

    def validate_founding_year(self, value):
        if value is not None:
            current_year = datetime.now().year
            if not (1800 <= value <= current_year): # Basic sanity check for year
                raise DRFValidationError(f"Founding year must be between 1800 and {current_year}.")
        return value

    def validate(self, data):
        # --- Email Validation ---
        for email_field in ['studentContactEmail']:
            if email_field in data and data[email_field]:
                try: validate_email(data[email_field])
                except DjangoValidationError: raise DRFValidationError({email_field: "Invalid email address"})

        # --- Time Validation ---
        if data.get('openingTime') and data.get('closingTime'):
            if data['openingTime'] >= data['closingTime']:
                raise DRFValidationError({"closingTime": "Closing time must be after opening time"})

        # --- AGREEMENTS Validation ---
        if not data.get('termsAccepted'):
            raise DRFValidationError({"termsAccepted": "You must accept the Terms of Service"})
        if not data.get('privacyAccepted'):
            raise DRFValidationError({"privacyAccepted": "You must accept the Privacy Policy"})
        
        liability_waiver = data.get('liabilityWaiver')
        if isinstance(liability_waiver, str): data['liabilityWaiver'] = liability_waiver.lower() == 'true'
        elif liability_waiver is None: raise DRFValidationError({"liabilityWaiver": "Liability waiver agreement is required."})
        elif not isinstance(liability_waiver, bool): raise DRFValidationError({"liabilityWaiver": "Invalid value for liability waiver."})
        if not data.get('liabilityWaiver'): raise DRFValidationError({"liabilityWaiver": "You must agree to the liability waiver."})

        # --- Coordinate Validation ---
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        if (latitude is not None and longitude is None) or \
           (longitude is not None and latitude is None):
             raise DRFValidationError("Both latitude and longitude must be provided together, or neither.")
        data['showExactLocation'] = (latitude is not None and longitude is not None)

        # --- List and Dict Field JSON Parsing ---
        json_string_fields = {
            'subcategories': list, 'classFormats': list, 'skillLevels': list, 
            'ageGroups': list, 'tags_keywords': list, 'social_media_links': dict
        }
        for field, expected_type in json_string_fields.items():
            field_value = data.get(field)
            if field_value:
                try:
                    parsed_value = json.loads(field_value)
                    if not isinstance(parsed_value, expected_type):
                        raise DRFValidationError({field: f"Invalid format. Expected a {expected_type.__name__}."})
                    data[field] = parsed_value
                except json.JSONDecodeError:
                    raise DRFValidationError({field: f"Invalid JSON format provided for {field}."})
                except TypeError: # Already parsed (e.g., not from FormData)
                    if not isinstance(field_value, expected_type):
                        raise DRFValidationError({field: f"Unexpected type for {field}. Expected JSON string or {expected_type.__name__}."})
                    data[field] = field_value
            elif field in data: # Field exists but is empty string or None
                 data[field] = expected_type() # Default to empty list/dict

        data['isActive'] = False
        return data

    def create(self, validated_data):
        user = self.context['request'].user
        validated_data['termsAccepted'] = validated_data.get('termsAccepted', False)
        validated_data['privacyAccepted'] = validated_data.get('privacyAccepted', False)
        validated_data['liabilityWaiver'] = validated_data.get('liabilityWaiver', False)
        validated_data.pop('isActive', None)

        business = BusinessInfo.objects.create(
            owner=user,
            verificationStatus='pending',
            isActive=False,
            **validated_data
        )
        VerificationRequest.objects.create(user=user, business=business, status='pending')
        return business

class ManagedBusinessInfoSerializer(serializers.ModelSerializer):
    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    managers_emails = serializers.SerializerMethodField(read_only=True)

    studentContactEmail = serializers.EmailField(required=False, allow_blank=True)
    studentContactPhone = serializers.CharField(required=False, allow_blank=True)
    
    businessImage = serializers.ImageField(required=False, allow_null=True, use_url=True)
    
    # Direct model fields for new additions (editable in settings)
    website = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    business_timezone = serializers.ChoiceField(choices=COMMON_TIMEZONE_CHOICES_SERIALIZER, required=False, allow_blank=True)
    social_media_links = serializers.JSONField(required=False) # Allow direct JSON editing
    tags_keywords = serializers.JSONField(required=False) # Allow direct JSON editing (list of strings)
    founding_year = serializers.IntegerField(required=False, allow_null=True)

    # Read-only representations of original JSONFields (if they are set at registration and not changed often)
    subcategories = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)
    classFormats = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)
    skillLevels = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)
    ageGroups = serializers.ListField(child=serializers.CharField(), read_only=True, required=False)

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'businessType', 'businessDescription',
            'businessImage', 
            'website', # ADDED
            'business_timezone', # ADDED
            'social_media_links', # ADDED
            'tags_keywords', # ADDED
            'founding_year', # ADDED
            'studentContactEmail', 'studentContactPhone',
            'businessAddress', 'businessCity', 'businessState', 'businessZipCode',
            'latitude', 'longitude', 'showExactLocation',
            'openingTime', 'closingTime', 
            'preferredContact',
            'newBookingNotification', 'cancellationNotification', 'reminderNotification', 'smsNotifications',
            'liabilityWaiver',
            'classCategory', 
            'subcategories','classFormats', 'skillLevels', 'ageGroups',
            'owner_email', 'managers_emails', 'verificationStatus',
            'stripe_account_id', 'stripe_account_status',
            'createdAt', 'updatedAt',
        ]
        read_only_fields = (
            'businessId', 'owner_email', 'managers_emails', 'verificationStatus',
            'stripe_account_id', 'stripe_account_status',
            'createdAt', 'updatedAt',
            'classCategory', 
            'subcategories', 'classFormats', 'skillLevels', 'ageGroups'
        )
        extra_kwargs = {
             'businessName': {'required': False}, 'businessType': {'required': False},
             'businessDescription': {'required': False},
             'studentContactEmail': {'required': False, 'allow_blank': True},
             'studentContactPhone': {'required': False, 'allow_blank': True},
             'website': {'required': False, 'allow_blank': True, 'allow_null': True},
             'business_timezone': {'required': False, 'allow_blank': True},
             'social_media_links': {'required': False}, # Allow empty dict
             'tags_keywords': {'required': False}, # Allow empty list
             'founding_year': {'required': False, 'allow_null': True},
             'businessAddress': {'required': False, 'allow_blank': True},
             'latitude': {'required': False, 'allow_null': True},
             'longitude': {'required': False, 'allow_null': True},
             'openingTime': {'required': False, 'allow_null': True},
             'closingTime': {'required': False, 'allow_null': True},
             'preferredContact': {'required': False},
             'liabilityWaiver': {'required': False},
        }

    def get_managers_emails(self, obj):
        if hasattr(obj, 'managers'): return [manager.email for manager in obj.managers.all()]
        return []

    def validate_studentContactEmail(self, value):
        if value:
            try: validate_email(value)
            except DjangoValidationError: raise DRFValidationError("Invalid business email address.")
        return value

    def validate_website(self, value):
        if value:
            validator = URLValidator()
            try: validator(value)
            except DjangoValidationError: raise DRFValidationError("Invalid URL format for website.")
        return value
    
    def validate_founding_year(self, value):
        if value is not None:
            current_year = datetime.now().year
            if not (1800 <= value <= current_year):
                raise DRFValidationError(f"Founding year must be between 1800 and {current_year}.")
        return value

    def validate_tags_keywords(self, value):
        if value is not None: # Allow null or empty list
            if not isinstance(value, list):
                raise DRFValidationError("Tags/Keywords must be a list.")
            if not all(isinstance(item, str) for item in value):
                raise DRFValidationError("All tags/keywords must be strings.")
        return value if value is not None else []


    def validate_social_media_links(self, value):
        if value is not None: # Allow null or empty dict
            if not isinstance(value, dict):
                raise DRFValidationError("Social media links must be a dictionary.")
            # Optional: validate keys (e.g., 'facebook', 'instagram') and URL format of values
            url_validator = URLValidator()
            for key, url_val in value.items():
                if not isinstance(key, str) or not isinstance(url_val, str):
                    raise DRFValidationError(f"Invalid format for social media link: {key}. Both key and URL must be strings.")
                if url_val: # Only validate if URL is not empty
                    try:
                        url_validator(url_val)
                    except DjangoValidationError:
                        raise DRFValidationError(f"Invalid URL for social media '{key}': {url_val}")
        return value if value is not None else {}


    def validate_openingTime(self, value):
        if isinstance(value, str):
            try: return time.fromisoformat(value)
            except ValueError: raise DRFValidationError("Invalid opening time format. Use HH:MM.")
        return value

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
        
        if 'showExactLocation' not in data:
            data['showExactLocation'] = (latitude is not None and longitude is not None)
        
        business_image_input = self.context['request'].data.get('businessImage', ...)
        if business_image_input == '': data['businessImage'] = None
        elif business_image_input is ...: data.pop('businessImage', None)
        
        return data

    def update(self, instance, validated_data):
        new_image_file = validated_data.pop('businessImage', ...)
        if new_image_file is None:
            if instance.businessImage:
                try: instance.businessImage.delete(save=False)
                except Exception as e: logger.warning(f"Error deleting old businessImage for {instance.pk}: {e}")
            instance.businessImage = None
        elif new_image_file is not ...:
            if instance.businessImage:
                 try: instance.businessImage.delete(save=False)
                 except Exception as e: logger.warning(f"Error deleting old businessImage for {instance.pk} before update: {e}")
            instance.businessImage = new_image_file

        if 'latitude' in validated_data and 'longitude' in validated_data:
            instance.latitude = validated_data.get('latitude', instance.latitude)
            instance.longitude = validated_data.get('longitude', instance.longitude)
            if 'showExactLocation' in validated_data:
                 instance.showExactLocation = validated_data.get('showExactLocation')
            else:
                 instance.showExactLocation = (instance.latitude is not None and instance.longitude is not None)
        
        # Handle JSON fields: social_media_links and tags_keywords
        # If they are in validated_data, update them.
        # The JSONField in the model will store them correctly.
        if 'social_media_links' in validated_data:
            instance.social_media_links = validated_data.get('social_media_links', instance.social_media_links)
        if 'tags_keywords' in validated_data:
            instance.tags_keywords = validated_data.get('tags_keywords', instance.tags_keywords)

        return super().update(instance, validated_data)


# Keep BusinessStatsSerializer as is, or update if new fields affect stats directly
class BusinessStatsSerializer(serializers.ModelSerializer):
    total_revenue = serializers.SerializerMethodField()
    total_students = serializers.SerializerMethodField()
    total_classes = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    recent_bookings = serializers.SerializerMethodField()
    registration_date = serializers.DateTimeField(source='createdAt', read_only=True)
    totalReviews = serializers.SerializerMethodField() 

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
        # Assuming Reviews model has a ForeignKey to BusinessInfo named 'business' or similar
        # If Reviews are linked via ClassesMain -> BusinessInfo, adjust the query
        # Example: return Reviews.objects.filter(classId__businessId=obj, status='approved').count()
        return obj.reviews_directly_to_business.filter(status='approved').count() # Use the direct relation if available

    def get_average_rating(self, obj):
        avg = obj.reviews_directly_to_business.filter(status='approved').aggregate(avg=Avg('rating'))['avg'] or 0.0
        return round(avg, 1)

    def get_total_revenue(self, obj):
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
        return Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=obj
        ).values('user').distinct().count()

    def get_total_classes(self, obj):
        # Assuming ClassesMain has ForeignKey 'businessId' to BusinessInfo
        return ClassesMain.objects.filter(businessId=obj, status='active').count()


    def get_recent_bookings(self, obj):
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=obj,
            booking_date__gte=thirty_days_ago,
            payment_status='paid'
        ).filter(
            Q(booking_group_id__isnull=True) |
            Q(id=Subquery(
                Booking.objects.filter(booking_group_id=OuterRef('booking_group_id'))
                .order_by('id').values('id')[:1]
            ))
        ).count()