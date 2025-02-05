from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
import traceback
from django.db.models import Sum, Count, F, ExpressionWrapper, FloatField, DecimalField, Q, Value, IntegerField, Case, When
from django.db.models.functions import TruncDate, ExtractMonth, ExtractYear, Coalesce, Cast
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from datetime import datetime
from rest_framework.exceptions import PermissionDenied, ValidationError
import csv
from django.http import HttpResponse

from ..utils.permissions import check_user_role
from ..models import Booking, ClassesMain, ClassOption, BusinessInfo

import logging
logger = logging.getLogger(__name__)

class RevenueAnalyticsView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_business(self, user):
        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(managers=user)
        ).first()
        
        if not business and not check_user_role(user, ['Admin', 'Super Admin']):
            raise PermissionDenied("No associated business found")
            
        return business

    def get_date_range(self, request):
        try:
            start_date = request.query_params.get('start_date')
            end_date = request.query_params.get('end_date')
            
            if start_date and end_date:
                # Convert to timezone-aware datetime
                start_date = timezone.make_aware(datetime.strptime(start_date, '%Y-%m-%d'))
                end_date = timezone.make_aware(datetime.strptime(end_date, '%Y-%m-%d'))
                
                return (
                    start_date.date(),
                    end_date.date()
                )
            
            # Default to current date range
            end_date = timezone.now().date()
            start_date = end_date - relativedelta(days=30)
            return start_date, end_date
            
        except ValueError as e:
            raise ValidationError("Invalid date format. Use YYYY-MM-DD")

    def calculate_metrics(self, business, start_date, end_date):
        # Log the date range being used
        logger.info(f"Calculating Metrics for Date Range: {start_date} to {end_date}")
        
        current_period = self.get_base_queryset(business).filter(
            booking_date__range=[start_date, end_date]
        )
        
        period_length = (end_date - start_date).days
        previous_start = start_date - relativedelta(days=period_length)
        previous_period = self.get_base_queryset(business).filter(
            booking_date__range=[previous_start, start_date]
        )
        
        logger.info(f"Current Period Bookings: {current_period.count()}")
        logger.info(f"Previous Period Bookings: {previous_period.count()}")
        
        current_metrics = current_period.aggregate(
            total_revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField()),
            total_students=Coalesce(Count('student', distinct=True), Value(0), output_field=IntegerField())
        )
        
        previous_metrics = previous_period.aggregate(
            prev_revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField()),
            prev_students=Coalesce(Count('student', distinct=True), Value(0), output_field=IntegerField())
        )
        
        logger.info(f"Current Metrics: {current_metrics}")
        logger.info(f"Previous Metrics: {previous_metrics}")
        
        return {
            'total_revenue': current_metrics['total_revenue'],
            'total_students': current_metrics['total_students'],
            'revenue_growth': self.calculate_growth(
                float(current_metrics['total_revenue']),
                float(previous_metrics['prev_revenue'])
            ),
            'student_growth': self.calculate_growth(
                current_metrics['total_students'],
                previous_metrics['prev_students']
            )
        }

    def get_base_queryset(self, business):
        # Prefetch related data to avoid N+1 queries
        return Booking.objects.select_related(
            'schedule_instance__schedule__option__classId',
            'student'
        ).filter(
            schedule_instance__schedule__option__classId__businessId=business,
            status='completed',
            payment_status='paid'
        )

    def calculate_growth(self, current, previous):
        if not previous:
            return float(100 if current else 0)
        return float(((current - previous) / previous) * 100) if previous else 0

    def get_time_series(self, business, start_date, end_date): 
        bookings = self.get_base_queryset(business).filter(
            booking_date__range=[start_date, end_date]
        )
        
        return bookings.annotate(
            date=TruncDate('booking_date')
        ).values('date').annotate(
            revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField()),
            students=Coalesce(Count('student', distinct=True), Value(0), output_field=IntegerField()),
            bookings=Coalesce(Count('id'), Value(0), output_field=IntegerField()),
            average_booking_value=ExpressionWrapper(
                Cast(Sum('amount_paid'), FloatField()) / Cast(Count('id'), FloatField()),
                output_field=FloatField()
            )
        ).order_by('date')

    def get_distribution(self, business, start_date, end_date):
        try:
            bookings = self.get_base_queryset(business).filter(
                booking_date__range=[start_date, end_date]
            )
            
            total_revenue = bookings.aggregate(
                total=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField())
            )['total']
            
            # Simplified distribution calculation
            class_distribution = []
            
            classes = ClassesMain.objects.filter(businessId=business)
            
            for class_obj in classes:
                # Calculate class-level revenue and students
                class_bookings = bookings.filter(
                    schedule_instance__schedule__option__classId=class_obj
                )
                
                class_total_revenue = class_bookings.aggregate(
                    revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField())
                )['revenue']
                
                class_total_students = class_bookings.aggregate(
                    students=Coalesce(Count('student', distinct=True), Value(0), output_field=IntegerField())
                )['students']
                
                # Calculate percentage only if total_revenue is not zero
                percentage = (float(class_total_revenue) / float(total_revenue) * 100) if total_revenue > 0 else 0
                
                # Prepare options distribution
                options = []
                class_options = ClassOption.objects.filter(classId=class_obj)
                
                for option in class_options:
                    option_bookings = class_bookings.filter(
                        schedule_instance__schedule__option=option
                    )
                    
                    option_revenue = option_bookings.aggregate(
                        revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField())
                    )['revenue']
                    
                    option_students = option_bookings.aggregate(
                        students=Coalesce(Count('student', distinct=True), Value(0), output_field=IntegerField())
                    )['students']
                    
                    # Calculate option percentage
                    option_percentage = (float(option_revenue) / float(class_total_revenue) * 100) if class_total_revenue > 0 else 0
                    
                    options.append({
                        'optionId': option.optionId,
                        'title': option.title,
                        'revenue': option_revenue,
                        'students': option_students,
                        'percentage': option_percentage
                    })
                
                # Only include classes with revenue
                if class_total_revenue > 0:
                    class_distribution.append({
                        'classId': class_obj.classId,
                        'title': class_obj.title,
                        'total_revenue': class_total_revenue,
                        'total_students': class_total_students,
                        'percentage': percentage,
                        'options': options
                    })
            
            return class_distribution
        
        except Exception as e:
            logger.error(f"Error in revenue distribution: {str(e)}")
            return []

    def get(self, request):
        try:
            business = self.get_business(request.user)
            start_date, end_date = self.get_date_range(request)
            
            # Add logging for debugging
            logger.info(f"Business: {business}")
            logger.info(f"Start Date: {start_date}, End Date: {end_date}")
            
            data = {
                'metrics': {
                    **self.calculate_metrics(business, start_date, end_date),
                    'businessId': business.businessId if business else None,
                    'businessName': business.businessName if business else None
                },
                'time_series': self.get_time_series(business, start_date, end_date),
                'distribution': self.get_distribution(business, start_date, end_date)
            }
            
            # Log the data before returning
            logger.info(f"Returned Data: {data}")
            
            return Response(data)
        
        except Exception as e:
            logger.error(f"Error in RevenueAnalytics GET: {str(e)}")
            return Response({
                'error': str(e),
                'details': str(traceback.format_exc())  # requires importing traceback
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        business = self.get_business(request.user)
        start_date, end_date = self.get_date_range(request)
        time_series = self.get_time_series(business, start_date, end_date)
        
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{business.businessName}_revenue_report.csv"'
        
        writer = csv.writer(response)
        writer.writerow(['Date', 'Revenue', 'Students', 'Bookings', 'Average Booking Value'])
        
        for entry in time_series:
            writer.writerow([
                entry['date'],
                entry['revenue'],
                entry['students'],
                entry['bookings'],
                entry['average_booking_value']
            ])
        
        return response