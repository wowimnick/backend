from rest_framework import serializers
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, Count, Avg
from ..models import BusinessInfo, Booking, ClassesMain, Reviews
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

class BusinessRegistrationSerializer(serializers.ModelSerializer):
    latitude = serializers.DecimalField(max_digits=10, decimal_places=8, required=False)
    longitude = serializers.DecimalField(max_digits=11, decimal_places=8, required=False)
    subcategories = serializers.ListField(child=serializers.CharField(), write_only=True)
    classFormats = serializers.ListField(child=serializers.CharField(), write_only=True)
    skillLevels = serializers.ListField(child=serializers.CharField(), write_only=True)
    ageGroups = serializers.ListField(child=serializers.CharField(), write_only=True)
    
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
            'showExactLocation', 'classCategory', 'subcategories',
            'classFormats', 'skillLevels', 'ageGroups',
            'verificationDocument', 'termsAccepted', 'privacyAccepted'
        ]

    def validate(self, data):
        # Validate emails
        for email_field in ['studentContactEmail', 'adminContactEmail']:
            if data.get(email_field):
                try:
                    validate_email(data[email_field])
                except ValidationError:
                    raise serializers.ValidationError({email_field: "Invalid email address"})

        # Validate business hours
        if data.get('openingTime') and data.get('closingTime'):
            if data['openingTime'] >= data['closingTime']:
                raise serializers.ValidationError(
                    {"closingTime": "Closing time must be after opening time"}
                )

        # Validate terms acceptance
        if not data.get('termsAccepted'):
            raise serializers.ValidationError(
                {"termsAccepted": "You must accept the terms and conditions"}
            )
        if not data.get('privacyAccepted'):
            raise serializers.ValidationError(
                {"privacyAccepted": "You must accept the privacy policy"}
            )

        # Convert list fields to comma-separated strings
        for field in ['subcategories', 'classFormats', 'skillLevels', 'ageGroups']:
            if field in data:
                data[field] = ','.join(data[field])

        return data

    def create(self, validated_data):
        user = self.context['request'].user
        return BusinessInfo.objects.create(owner=user, **validated_data)

class BusinessInfoSerializer(serializers.ModelSerializer):
    classes_count = serializers.IntegerField(read_only=True)
    bookings_count = serializers.IntegerField(read_only=True)
    revenue = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    status = serializers.CharField(read_only=True)
    rating = serializers.FloatField(read_only=True)
    
    class Meta:
        model = BusinessInfo
        fields = '__all__'
        
    def to_representation(self, instance):
        """
        Customize the output based on user authentication status
        """
        data = super().to_representation(instance)
        request = self.context.get('request')
        
        if not request or not request.user.is_authenticated:
            # Remove sensitive fields for public view
            sensitive_fields = [
                'adminContactPhone', 'adminContactEmail',
                'verificationDocument', 'verificationStatus',
                'managers'
            ]
            for field in sensitive_fields:
                data.pop(field, None)
                
            # Only include exact location if allowed
            if not instance.showExactLocation:
                data.pop('latitude', None)
                data.pop('longitude', None)
                
        return data

class BusinessStatsSerializer(serializers.ModelSerializer):
    total_revenue = serializers.SerializerMethodField()
    total_students = serializers.SerializerMethodField()
    total_classes = serializers.SerializerMethodField()
    total_instructors = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    recent_bookings = serializers.SerializerMethodField()
    class_categories = serializers.SerializerMethodField()
    registration_date = serializers.DateTimeField(source='createdAt')

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'businessImage', 'totalReviews', 
            'total_revenue', 'total_students', 'total_classes',
            'total_instructors', 'average_rating', 'recent_bookings',
            'class_categories', 'registration_date'
        ]

    def get_average_rating(self, obj):
        return Reviews.objects.filter(
            classId__businessId=obj
        ).aggregate(
            avg=Avg('rating')
        )['avg'] or 0

    def get_total_revenue(self, obj):
        return Booking.objects.filter(
            class_instance__businessId=obj,
            status__name='Completed'
        ).aggregate(
            total=Sum('class_instance__classPrice')
        )['total'] or 0

    def get_total_students(self, obj):
        return Booking.objects.filter(
            class_instance__businessId=obj
        ).values('student').distinct().count()

    def get_total_classes(self, obj):
        return ClassesMain.objects.filter(
            businessId=obj,
            isActive=True
        ).count()

    def get_total_instructors(self, obj):
        return obj.instructors.count()

    def get_recent_bookings(self, obj):
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return Booking.objects.filter(
            class_instance__businessId=obj,
            booking_date__gte=thirty_days_ago
        ).count()

    def get_class_categories(self, obj):
        return ClassesMain.objects.filter(
            businessId=obj
        ).values('classCategory').annotate(
            count=Count('classId')
        )