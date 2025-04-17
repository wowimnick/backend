from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework.exceptions import ValidationError as DRFValidationError
from decimal import Decimal

from ...models import BusinessInfo, ClassesMain, Reviews, CustomUser
from django.db.models import Sum, Count, Avg, Q
from datetime import timedelta
from django.utils import timezone
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
    latitude = serializers.DecimalField(max_digits=10, decimal_places=8, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=11, decimal_places=8, required=False, allow_null=True)
    subcategories = serializers.ListField(child=serializers.CharField(), write_only=True, required=False, allow_empty=True)
    classFormats = serializers.ListField(child=serializers.CharField(), write_only=True, required=False, allow_empty=True)
    skillLevels = serializers.ListField(child=serializers.CharField(), write_only=True, required=False, allow_empty=True)
    ageGroups = serializers.ListField(child=serializers.CharField(), write_only=True, required=False, allow_empty=True)
    verificationDocument = serializers.FileField(required=False, allow_null=True)

    class Meta:
        model = BusinessInfo
        fields = [
            'businessName', 'businessType', 'businessDescription',
            'businessImage', 'openingTime', 'closingTime',
            'cancellationPolicy', 'liabilityWaiver',
            'studentContactPhone', 'studentContactEmail',
            'adminContactPhone', 'adminContactEmail',
            'preferredContact', 'businessAddress', 'businessCity',
            'businessState', 'businessZipCode', 'latitude', 'longitude',
            'showExactLocation', 'classCategory',
            'subcategories', 'classFormats', 'skillLevels', 'ageGroups',
            'verificationDocument', 'termsAccepted', 'privacyAccepted'
        ]
        # Keep extra_kwargs as before or adjust as needed
        extra_kwargs = {
            'businessName': {'required': True}, 'businessType': {'required': True},
            'businessDescription': {'required': True}, 'openingTime': {'required': True},
            'closingTime': {'required': True}, 'cancellationPolicy': {'required': True},
            'studentContactPhone': {'required': True}, 'studentContactEmail': {'required': True},
            'preferredContact': {'required': True}, 'businessAddress': {'required': True},
            'businessCity': {'required': True}, 'businessState': {'required': True},
            'businessZipCode': {'required': True}, 'classCategory': {'required': True},
            'termsAccepted': {'required': True}, 'privacyAccepted': {'required': True},
            'adminContactPhone': {'required': False, 'allow_blank': True, 'allow_null': True},
            'adminContactEmail': {'required': False, 'allow_blank': True, 'allow_null': True},
            'businessImage': {'required': False, 'allow_null': True},
        }

    def validate(self, data):
        # Keep validation logic
        for email_field in ['studentContactEmail', 'adminContactEmail']:
            if data.get(email_field):
                try:
                    validate_email(data[email_field])
                except DjangoValidationError:
                    raise DRFValidationError({email_field: "Invalid email address"})
        if data.get('openingTime') and data.get('closingTime'):
            if data['openingTime'] >= data['closingTime']:
                raise DRFValidationError({"closingTime": "Closing time must be after opening time"})
        if not data.get('termsAccepted'):
            raise DRFValidationError({"termsAccepted": "You must accept the terms and conditions"})
        if not data.get('privacyAccepted'):
            raise DRFValidationError({"privacyAccepted": "You must accept the privacy policy"})

        # Convert list fields (no need to pop them here, handled in create)
        for field in ['subcategories', 'classFormats', 'skillLevels', 'ageGroups']:
            if field in data and isinstance(data[field], list):
                # Store the list temporarily for create/update to handle joining
                setattr(self, f'_{field}_list', data[field])

        # Handle lat/lon (no change needed here)
        latitude = data.get('latitude')
        longitude = data.get('longitude')
        if latitude is not None and longitude is None:
             raise DRFValidationError({"longitude": "Longitude is required if latitude is provided."})
        if longitude is not None and latitude is None:
             raise DRFValidationError({"latitude": "Latitude is required if longitude is provided."})

        return data

    def create(self, validated_data):
        user = self.context['request'].user

        # Join list fields retrieved from validate (or directly from validated_data if not popped)
        def join_list_field(field_name):
            lst = getattr(self, f'_{field_name}_list', validated_data.pop(field_name, []))
            return ','.join(filter(None, lst or []))

        subcategories_str = join_list_field('subcategories')
        classFormats_str = join_list_field('classFormats')
        skillLevels_str = join_list_field('skillLevels')
        ageGroups_str = join_list_field('ageGroups')

        business = BusinessInfo.objects.create(
            owner=user,
            subcategories=subcategories_str,
            classFormats=classFormats_str,
            skillLevels=skillLevels_str,
            ageGroups=ageGroups_str,
            verificationStatus='pending',
            **validated_data
        )
        return business

class ManagedBusinessInfoSerializer(serializers.ModelSerializer):
    """
    Serializer for Business Owners/Managers viewing/editing their OWN profile.
    Simplified for Canada-only, single-session launch.
    """
    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    managers_emails = serializers.SerializerMethodField(read_only=True)

    # Mapped fields
    email = serializers.EmailField(source='studentContactEmail', required=False)
    phone = serializers.CharField(source='studentContactPhone', required=False)
    location = serializers.CharField(source='businessAddress', required=False)
    saltLocation = serializers.BooleanField(write_only=True, required=False)

    # Direct model fields (subset relevant to settings UI)
    openingTime = serializers.TimeField(format='%H:%M', required=False, allow_null=True)
    closingTime = serializers.TimeField(format='%H:%M', required=False, allow_null=True)
    latitude = serializers.DecimalField(max_digits=10, decimal_places=8, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=11, decimal_places=8, required=False, allow_null=True)
    businessImage = serializers.ImageField(required=False, allow_null=True, use_url=True)

    class Meta:
        model = BusinessInfo
        # List only the fields the frontend settings component will interact with
        fields = [
            'businessId',
            'businessName',
            'businessType',
            'businessDescription',
            'businessImage',
            'website',

            # General Contact (mapped)
            'email',
            'phone',

            # Location
            'location', # source='businessAddress'
            'latitude',
            'longitude',
            'showExactLocation', # Read-only display
            'saltLocation', # Write-only helper

            # Simplified Booking
            'cancellationPolicy',
            'refundPolicy',
            # REMOVED: advanceBookingDays, instantBooking

            # Preferences
            'openingTime',
            'closingTime',
            # REMOVED: timeZone, weekStartsOn
            'newBookingNotification',
            'cancellationNotification',
            'reminderNotification',
            'smsNotifications',

            # Payment - REMOVED currency, taxRate, invoicePrefix

            # Read-only context fields
            'owner_email',
            'managers_emails',
            'verificationStatus',
            'stripe_account_id', # Keep for future use
            'stripe_account_status',# Keep for future use
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
        )
        # Keep extra_kwargs minimal or remove if defaults are fine
        extra_kwargs = {
             'businessName': {'required': False},
             'businessType': {'required': False},
             'businessDescription': {'required': False},
             'openingTime': {'required': False},
             'closingTime': {'required': False},
             'cancellationPolicy': {'required': False},
             'refundPolicy': {'required': False},
        }

    # Methods for computed/read-only fields
    def get_managers_emails(self, obj):
        if hasattr(obj, 'managers'):
             return [manager.email for manager in obj.managers.all()]
        return []

    def get_classes_count(self, obj):
        # Note: Annotate in view is better for performance if used in lists
        return obj.classesmain_set.count() # Use default related name

    def get_average_rating(self, obj):
         # Note: Annotate in view is better for performance if used in lists
         avg = obj.reviews_set.filter(status='approved').aggregate(Avg('rating'))['rating__avg'] # Use default related name
         return round(avg, 1) if avg else 0.0

    def _split_string_to_list(self, data_string):
         if data_string and isinstance(data_string, str):
             return [item.strip() for item in data_string.split(',') if item.strip()]
         return []

    def get_subcategories_list(self, obj):
        return self._split_string_to_list(obj.subcategories)

    def get_classFormats_list(self, obj):
        return self._split_string_to_list(obj.classFormats)

    def get_skillLevels_list(self, obj):
        return self._split_string_to_list(obj.skillLevels)

    def get_ageGroups_list(self, obj):
        return self._split_string_to_list(obj.ageGroups)

    def validate_email(self, value):
        try:
            validate_email(value)
        except DjangoValidationError:
            raise DRFValidationError("Invalid email address provided.")
        return value
    
    def validate(self, data):
        opening = data.get('openingTime', getattr(self.instance, 'openingTime', None))
        closing = data.get('closingTime', getattr(self.instance, 'closingTime', None))
        if opening and closing and opening >= closing:
            raise DRFValidationError({"closingTime": "Closing time must be after opening time."})

        if 'saltLocation' in data:
            data['showExactLocation'] = not data['saltLocation']

        business_image_request = self.context['request'].data.get('businessImage')
        if business_image_request == '' and 'businessImage' not in data:
             data['businessImage'] = None

        return data

    def update(self, instance, validated_data):
        new_image = validated_data.get('businessImage', ...)
        if new_image is None:
            if instance.businessImage:
                instance.businessImage.delete(save=False)
            instance.businessImage = None
            validated_data.pop('businessImage')
        elif new_image is not ...:
             if instance.businessImage and instance.businessImage != new_image:
                 instance.businessImage.delete(save=False)

        validated_data.pop('saltLocation', None)
        return super().update(instance, validated_data)


# Keep BusinessStatsSerializer here as it's used by the BusinessDashboardViewSet
class BusinessStatsSerializer(serializers.ModelSerializer):
    total_revenue = serializers.SerializerMethodField()
    total_students = serializers.SerializerMethodField()
    total_classes = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    recent_bookings = serializers.SerializerMethodField()
    registration_date = serializers.DateTimeField(source='createdAt', read_only=True)

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'businessImage',
            'totalReviews',
            'total_revenue', 'total_students', 'total_classes',
            'average_rating', 'recent_bookings',
            'registration_date'
        ]

    def get_average_rating(self, obj):
        # Ensure Reviews model is imported or use obj.reviews_set
        avg = obj.reviews_set.filter(status='approved').aggregate(avg=Avg('rating'))['avg'] or 0.0
        return round(avg, 1)

    def get_total_revenue(self, obj):
        # Ensure Booking model is imported or use obj.booking_set if related name exists
        # Requires careful path definition from Booking -> BusinessInfo
        # Assuming Booking relates via Class -> BusinessInfo
        from ...models import Booking # Adjust import
        total = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=obj,
            status='completed',
            payment_status='paid'
        ).aggregate(total=Sum('amount_paid'))['total'] or Decimal('0.00')
        return total

    def get_total_students(self, obj):
        from ...models import Booking # Adjust import
        return Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=obj
        ).values('user').distinct().count()

    def get_total_classes(self, obj):
        # Use default related name
        return obj.classesmain_set.filter(status='active').count()

    def get_recent_bookings(self, obj):
        from ...models import Booking # Adjust import
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=obj,
            booking_date__gte=thirty_days_ago
        ).count()