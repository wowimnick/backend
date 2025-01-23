from rest_framework import serializers
from django.db.models import Sum, Count, F, ExpressionWrapper, FloatField
from django.db.models.functions import TruncDate, ExtractMonth, ExtractYear
from django.utils import timezone
from dateutil.relativedelta import relativedelta

from ..models import Booking, ClassesMain, ClassOption, BusinessInfo

class RevenueMetricsSerializer(serializers.ModelSerializer):
    total_revenue = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total_students = serializers.IntegerField(read_only=True)
    revenue_growth = serializers.FloatField(read_only=True)
    student_growth = serializers.FloatField(read_only=True)
    
    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'total_revenue', 'total_students',
            'revenue_growth', 'student_growth'
        ]

class RevenueTimeSeriesSerializer(serializers.Serializer):
    date = serializers.DateField()
    revenue = serializers.DecimalField(max_digits=10, decimal_places=2)
    students = serializers.IntegerField()
    bookings = serializers.IntegerField()
    average_booking_value = serializers.DecimalField(max_digits=10, decimal_places=2)

class RevenueDistributionSerializer(serializers.Serializer):
    class_id = serializers.IntegerField(source='classId')
    class_title = serializers.CharField(source='title')
    total_revenue = serializers.DecimalField(max_digits=10, decimal_places=2)
    total_students = serializers.IntegerField()
    percentage = serializers.FloatField()
    
    class OptionDistributionSerializer(serializers.Serializer):
        option_id = serializers.IntegerField(source='optionId')
        option_title = serializers.CharField(source='title')
        revenue = serializers.DecimalField(max_digits=10, decimal_places=2)
        students = serializers.IntegerField()
        percentage = serializers.FloatField()
    
    options = OptionDistributionSerializer(many=True)

class RevenueReportSerializer(serializers.Serializer):
    metrics = RevenueMetricsSerializer()
    time_series = RevenueTimeSeriesSerializer(many=True)
    distribution = RevenueDistributionSerializer(many=True)