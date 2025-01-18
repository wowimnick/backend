from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from ..models import Booking, Schedule
from datetime import datetime
from django.utils import timezone
from django.db import transaction

class BookingCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = ['schedule_instance', 'participants', 'notes']
        
    def validate(self, data):
        instance = data['schedule_instance']
        participants = data['participants']
        
        # Check if instance is available
        if instance.status != 'scheduled':
            raise serializers.ValidationError(
                'This class instance is not available for booking'
            )
            
        # Check if booking is in the past
        if instance.date < timezone.now().date():
            raise serializers.ValidationError(
                'Cannot book past class instances'
            )
            
        # Check capacity
        if not instance.can_accommodate(participants):
            raise serializers.ValidationError(
                f'Only {instance.available_spots} spots remaining'
            )
            
        return data

    def create(self, validated_data):
        with transaction.atomic():
            # Create the booking
            booking = Booking.objects.create(
                **validated_data,
                amount_paid=validated_data['schedule_instance'].price * validated_data['participants']
            )
            
            # Here you would typically integrate with payment processing
            # For now, we'll just confirm the booking
            booking.status = 'confirmed'
            booking.payment_status = 'paid'
            booking.save()
            
            return booking

class BookingDetailSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    student_email = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    business_name = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()
    
    class Meta:
        model = Booking
        fields = [
            'id', 'schedule_instance', 'date', 'time',
            'student_name', 'student_email', 'class_name', 
            'business_name', 'participants', 'notes', 'status',
            'booking_date', 'cancelled_at', 'cancellation_reason',
            'amount_paid', 'payment_status'
        ]
    
    def get_date(self, obj):
        return obj.schedule_instance.date if obj.schedule_instance else None

    def get_time(self, obj):
        return obj.schedule_instance.time if obj.schedule_instance else None
        
    def get_student_name(self, obj):
        if obj.student and obj.student.user:
            return f"{obj.student.user.first_name} {obj.student.user.last_name}".strip()
        return None

    def get_student_email(self, obj):
        return obj.student.user.email if obj.student and obj.student.user else None

    def get_class_name(self, obj):
        if obj.schedule_instance and obj.schedule_instance.schedule:
            return obj.schedule_instance.schedule.option.classId.title
        return None

    def get_business_name(self, obj):
        if obj.schedule_instance and obj.schedule_instance.schedule:
            return obj.schedule_instance.schedule.option.classId.businessId.businessName
        return None