from rest_framework import serializers
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, Count, Avg
from ..models import BusinessInfo, Booking, ClassesMain, Reviews
from .class_serializers import ClassesMainSerializer

class BusinessInfoSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessInfo
        fields = '__all__'

class BusinessStatsSerializer(serializers.ModelSerializer):
    total_revenue = serializers.SerializerMethodField()
    total_students = serializers.SerializerMethodField()
    total_classes = serializers.SerializerMethodField()
    total_instructors = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    recent_bookings = serializers.SerializerMethodField()
    class_categories = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'totalReviews', 
            'total_revenue', 'total_students', 'total_classes',
            'total_instructors', 'average_rating', 'recent_bookings',
            'class_categories'
        ]

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

    def get_average_rating(self, obj):
        return Reviews.objects.filter(
            classId__businessId=obj
        ).aggregate(
            avg=Avg('rating')
        )['avg'] or 0

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