from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import (
    Sum, Count, F, ExpressionWrapper, FloatField, DecimalField,
    Q, Value, Case, When, IntegerField
)
from django.db.models.functions import (
    TruncDate, ExtractMonth, ExtractYear, Coalesce
)
from django.utils import timezone
from datetime import datetime, timedelta
from rest_framework.exceptions import ValidationError
import csv
from django.http import HttpResponse
import logging

from quickstart.models import Booking, BusinessInfo, ClassOption, ClassesMain

logger = logging.getLogger(__name__)

class RevenueAnalyticsView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_business(self, user):
        """Get user's business with permission check."""
        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(managers=user)
        ).first()
        
        if not business:
            raise ValidationError("No associated business found")
            
        return business

    def get_date_range(self, request):
        """Parse and validate date range from request."""
        try:
            start_date = request.query_params.get('start_date')
            end_date = request.query_params.get('end_date')
            
            if start_date and end_date:
                start_date = timezone.make_aware(datetime.strptime(start_date, '%Y-%m-%d'))
                end_date = timezone.make_aware(datetime.strptime(end_date, '%Y-%m-%d'))
            else:
                end_date = timezone.now()
                start_date = end_date - timedelta(days=30)
            
            return start_date.date(), end_date.date()
            
        except ValueError as e:
            raise ValidationError("Invalid date format. Use YYYY-MM-DD")

    def calculate_metrics(self, business, start_date, end_date):
        """Calculate key revenue metrics."""
        current_period = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            booking_date__range=[start_date, end_date],
            payment_status='paid'
        )

        # Previous period for comparison
        period_length = (end_date - start_date).days
        previous_start = start_date - timedelta(days=period_length)
        previous_period = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            booking_date__range=[previous_start, start_date],
            payment_status='paid'
        )

        # Current period aggregations
        current_metrics = current_period.aggregate(
            total_revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField()),
            total_bookings=Count('id'),
            recurring_revenue=Coalesce(
                Sum('amount_paid', 
                    filter=Q(enrollment_type='Recurring Classes')),
                Value(0),
                output_field=DecimalField()
            )
        )

        # Previous period aggregations
        previous_metrics = previous_period.aggregate(
            prev_revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField()),
            prev_bookings=Count('id')
        )

        # Calculate revenue per student
        unique_students = current_period.values('student').distinct().count()
        revenue_per_student = (
            float(current_metrics['total_revenue']) / unique_students
            if unique_students > 0 else 0
        )

        # Calculate average order value
        aov = (
            float(current_metrics['total_revenue']) / current_metrics['total_bookings']
            if current_metrics['total_bookings'] > 0 else 0
        )

        # Calculate growth rates
        revenue_growth = (
            ((float(current_metrics['total_revenue']) - float(previous_metrics['prev_revenue'])) 
             / float(previous_metrics['prev_revenue']) * 100)
            if previous_metrics['prev_revenue'] > 0 else 0
        )

        return {
            'total_revenue': float(current_metrics['total_revenue']),
            'average_order_value': round(aov, 2),
            'recurring_revenue': float(current_metrics['recurring_revenue']),
            'revenue_per_student': round(revenue_per_student, 2),
            'revenue_growth': round(revenue_growth, 2)
        }

    def get_revenue_trends(self, business, start_date, end_date):
        """Get daily revenue trends with recurring vs one-time breakdown."""
        trends = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            booking_date__range=[start_date, end_date],
            payment_status='paid'
        ).annotate(
            date=TruncDate('booking_date')
        ).values('date').annotate(
            recurring_revenue=Coalesce(
                Sum('amount_paid',
                    filter=Q(enrollment_type='Recurring Classes')),
                Value(0),
                output_field=DecimalField()
            ),
            one_time_revenue=Coalesce(
                Sum('amount_paid',
                    filter=Q(enrollment_type='Single Session')),
                Value(0),
                output_field=DecimalField()
            )
        ).order_by('date')

        return [
            {
                'date': entry['date'].isoformat(),
                'recurring_revenue': float(entry['recurring_revenue']),
                'one_time_revenue': float(entry['one_time_revenue'])
            }
            for entry in trends
        ]

    def get_class_revenue(self, business, start_date, end_date):
        """Get revenue breakdown by class and options."""
        class_revenue = []
        
        classes = ClassesMain.objects.filter(businessId=business)
        
        for class_obj in classes:
            class_total = Booking.objects.filter(
                schedule_instance__schedule__option__classId=class_obj,
                booking_date__range=[start_date, end_date],
                payment_status='paid'
            ).aggregate(
                revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField())
            )['revenue']
            
            if class_total > 0:
                class_revenue.append({
                    'name': class_obj.title,
                    'revenue': float(class_total),
                    'id': class_obj.classId,
                    'type': 'class'
                })
                
                options = ClassOption.objects.filter(classId=class_obj)
                for option in options:
                    option_revenue = Booking.objects.filter(
                        schedule_instance__schedule__option=option,
                        booking_date__range=[start_date, end_date],
                        payment_status='paid'
                    ).aggregate(
                        revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField())
                    )['revenue']
                    
                    if option_revenue > 0:
                        class_revenue.append({
                            'name': f"  • {option.title}",
                            'revenue': float(option_revenue),
                            'id': f"{class_obj.classId}-{option.optionId}",
                            'type': 'option'
                        })
        
        return sorted(class_revenue, key=lambda x: x['revenue'], reverse=True)

    def get(self, request):
        """Handle GET request for revenue analytics."""
        try:
            business = self.get_business(request.user)
            start_date, end_date = self.get_date_range(request)
            
            data = {
                'metrics': self.calculate_metrics(business, start_date, end_date),
                'revenue_trends': self.get_revenue_trends(business, start_date, end_date),
                'class_revenue': self.get_class_revenue(business, start_date, end_date)
            }
            
            return Response(data)
            
        except Exception as e:
            logger.error(f"Error in revenue analytics: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def post(self, request):
        """Handle POST request for revenue report export."""
        try:
            business = self.get_business(request.user)
            start_date, end_date = self.get_date_range(request)
            
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = f'attachment; filename="{business.businessName}_revenue_report.csv"'
            
            writer = csv.writer(response)
            
            # Write daily revenue data
            writer.writerow(['Daily Revenue'])
            writer.writerow(['Date', 'Recurring Revenue', 'One-time Revenue', 'Total Revenue'])
            
            revenue_trends = self.get_revenue_trends(business, start_date, end_date)
            for entry in revenue_trends:
                writer.writerow([
                    entry['date'],
                    entry['recurring_revenue'],
                    entry['one_time_revenue'],
                    entry['recurring_revenue'] + entry['one_time_revenue']
                ])
            
            # Add separation
            writer.writerow([])
            
            # Write class revenue data
            writer.writerow(['Revenue by Class'])
            writer.writerow(['Class/Option', 'Revenue'])
            
            class_revenue = self.get_class_revenue(business, start_date, end_date)
            for entry in class_revenue:
                writer.writerow([entry['name'], entry['revenue']])
            
            return response
            
        except Exception as e:
            logger.error(f"Error exporting revenue report: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )