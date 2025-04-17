from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission # Added BasePermission
from django.db.models import (
    Sum, Count, F, ExpressionWrapper, FloatField, DecimalField,
    Q, Value, Case, When, IntegerField, Subquery, OuterRef, Min
)
from django.db.models.functions import (
    TruncDate, ExtractMonth, ExtractYear, Coalesce
)
from django.utils import timezone
from datetime import datetime, timedelta
from rest_framework.exceptions import ValidationError, PermissionDenied
import csv
from django.http import HttpResponse
import logging

from quickstart.models import Booking, BusinessInfo, ClassOption, ClassesMain, CustomUser # Added CustomUser

logger = logging.getLogger(__name__)

class RevenueAnalyticsView(views.APIView):
    """
    Provides revenue analytics for the user's associated business.
    Requires 'view_business_revenue_analytics' permission.
    Export requires 'export_business_revenue_data' permission.
    """
    permission_classes = [IsAuthenticated] # Base: User must be logged in

    def get_business(self, user):
        """Get user's associated business. Returns None if not found."""
        # Use Q object for cleaner OR condition
        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(managers=user)
        ).first()
        return business # Return None if no business found

    def get_date_range(self, request):
        """Parse and validate date range from request query parameters."""
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')

        try:
            if start_date_str and end_date_str:
                # Use timezone aware datetimes for range queries
                start_date = timezone.make_aware(datetime.strptime(start_date_str, '%Y-%m-%d'))
                # Ensure end_date includes the whole day
                end_date = timezone.make_aware(datetime.strptime(end_date_str, '%Y-%m-%d')) + timedelta(days=1) - timedelta.resolution
            else:
                # Default to the last 30 days
                end_date = timezone.now()
                start_date = end_date - timedelta(days=30)

            return start_date, end_date

        except ValueError:
            # Raise DRF's ValidationError for standard 400 response
            raise ValidationError("Invalid date format. Please use YYYY-MM-DD.")


    def get_valid_bookings_queryset(self, business, start_date, end_date):
        """
        Get the base queryset for valid bookings used in revenue calculation.
        Filters by business, date range, and 'paid' payment status.
        Handles course bookings by counting only the first booking in a group for revenue.
        """
        # Ensure business is provided
        if not business:
             return Booking.objects.none()

        base_bookings = Booking.objects.filter(
            # Filter by the specific business
            schedule_instance__schedule__option__classId__businessId=business,
            # Use booking_date for financial analysis period
            booking_date__range=[start_date, end_date],
            payment_status='paid' # Crucial: Only count paid bookings for revenue
        ).select_related( # Optimize related lookups needed later
            'schedule_instance__schedule__option__classId', # For class info
            'schedule_instance__schedule__option', # For option info
            'user' # For user counts
        )

        # Identify the earliest booking ID for each course booking group within the period
        # Ensure the subquery only considers bookings *within the date range and for this business*
        first_course_booking_ids = Subquery(
            Booking.objects.filter(
                booking_group_id=OuterRef('booking_group_id'), # Correlate by group id
                schedule_instance__schedule__option__classId__businessId=business, # Filter subquery by business
                booking_date__range=[start_date, end_date], # Filter subquery by date
                payment_status='paid' # Filter subquery by payment status
            )
            .order_by('id') # Get the first one based on ID (proxy for time)
            .values('id')[:1] # Select just the ID of the first one
        )

        # Filter: Include non-course bookings OR the first booking of each course group
        valid_bookings_qs = base_bookings.filter(
            Q(booking_group_id__isnull=True) | # All single session bookings
            Q(id=first_course_booking_ids)   # Only the first booking per course (matched by subquery)
        )

        return valid_bookings_qs


    def calculate_metrics(self, business, start_date, end_date):
        """Calculate key revenue metrics based on valid bookings."""
        current_period_qs = self.get_valid_bookings_queryset(business, start_date, end_date)

        # Previous period calculation
        period_length_days = (end_date.date() - start_date.date()).days + 1 # Inclusive range based on dates
        previous_end_date = start_date - timedelta.resolution # End of previous day
        previous_start_date = start_date - timedelta(days=period_length_days)
        previous_period_qs = self.get_valid_bookings_queryset(business, previous_start_date, previous_end_date)

        # --- Current Period Aggregations ---
        current_aggregates = current_period_qs.aggregate(
            total_revenue=Coalesce(Sum('amount_paid'), Value(0.0), output_field=DecimalField()),
            total_bookings=Count('id'), # Counts unique valid bookings (first per course)
            # Count distinct users associated with these valid bookings
            unique_users=Count('user', distinct=True)
        )

        current_total_revenue = float(current_aggregates['total_revenue'])
        current_total_bookings = current_aggregates['total_bookings']
        current_unique_users = current_aggregates['unique_users']


        # --- Previous Period Aggregations ---
        previous_aggregates = previous_period_qs.aggregate(
            prev_revenue=Coalesce(Sum('amount_paid'), Value(0.0), output_field=DecimalField())
        )
        previous_total_revenue = float(previous_aggregates['prev_revenue'])


        # --- Calculate Metrics ---
        average_order_value = (current_total_revenue / current_total_bookings) if current_total_bookings > 0 else 0.0
        revenue_per_user = (current_total_revenue / current_unique_users) if current_unique_users > 0 else 0.0

        # Calculate percentage growth
        revenue_growth = 0.0
        if previous_total_revenue > 0:
            revenue_growth = ((current_total_revenue - previous_total_revenue) / previous_total_revenue) * 100.0

        # --- Recurring Revenue Placeholder ---
        recurring_revenue = 0.0 # Adjust based on your business model

        return {
            'total_revenue': round(current_total_revenue, 2),
            'average_order_value': round(average_order_value, 2),
            'revenue_per_user': round(revenue_per_user, 2),
            'revenue_growth': round(revenue_growth, 1),
            'recurring_revenue': round(float(recurring_revenue), 2)
        }


    def get_revenue_trends(self, business, start_date, end_date):
        """Get daily or monthly revenue trends based on valid bookings."""
        valid_bookings_qs = self.get_valid_bookings_queryset(business, start_date, end_date)

        # Determine granularity
        days_diff = (end_date.date() - start_date.date()).days
        if days_diff <= 90:
            # Daily trends
            trends = valid_bookings_qs.annotate(
                date=TruncDate('booking_date')
            ).values('date').annotate(
                revenue=Coalesce(Sum('amount_paid'), Value(0.0), output_field=DecimalField())
            ).order_by('date')
            granularity = 'daily'
        else:
            # Monthly trends
            trends = valid_bookings_qs.annotate(
                year=ExtractYear('booking_date'),
                month=ExtractMonth('booking_date'),
            ).values('year', 'month').annotate(
                revenue=Coalesce(Sum('amount_paid'), Value(0.0), output_field=DecimalField())
            ).order_by('year', 'month')
            granularity = 'monthly'


        # Format results
        formatted_trends = []
        if granularity == 'daily':
            formatted_trends = [
                {'date': entry['date'].isoformat(), 'revenue': float(entry['revenue'])}
                for entry in trends
            ]
        else: # monthly
            formatted_trends = [
                {'date': f"{entry['year']}-{entry['month']:02d}-01", 'revenue': float(entry['revenue'])}
                for entry in trends
            ]

        return formatted_trends

    def get_class_revenue(self, business, start_date, end_date):
        """Get revenue breakdown by ClassesMain."""
        valid_bookings_qs = self.get_valid_bookings_queryset(business, start_date, end_date)

        # Aggregate revenue per Class ID directly
        class_revenue_data = valid_bookings_qs.values(
            'schedule_instance__schedule__option__classId' # Group by ClassMain ID
        ).annotate(
            total_revenue=Coalesce(Sum('amount_paid'), Value(0.0), output_field=DecimalField())
        ).order_by('-total_revenue') # Order by highest revenue

        # Fetch class titles efficiently
        class_ids = [item['schedule_instance__schedule__option__classId'] for item in class_revenue_data]
        class_titles = dict(ClassesMain.objects.filter(classId__in=class_ids).values_list('classId', 'title'))

        # Combine data
        result = []
        for item in class_revenue_data:
             class_id = item['schedule_instance__schedule__option__classId']
             if class_id: # Ensure class_id is not None
                 result.append({
                     'id': class_id,
                     'name': class_titles.get(class_id, f"Class ID {class_id}"), # Use fetched title
                     'revenue': float(item['total_revenue']),
                     'type': 'class' # Indicate this is a top-level class
                 })

        return result


    def get(self, request):
        """Handle GET request for revenue analytics."""
        user = request.user
        business = self.get_business(user) # Find the business context

        if not business:
            # If user is not associated with any business, deny access.
            # Platform admins might need a different view/logic if they should see aggregated stats.
            raise PermissionDenied("You are not associated with a business.")

        # Permission Check: Does user have permission for THEIR business analytics?
        if not user.has_perm('quickstart.view_business_revenue_analytics'):
            raise PermissionDenied("You do not have permission to view revenue analytics for this business.")

        try:
            start_date, end_date = self.get_date_range(request)

            # Pass the specific business object to calculation methods
            metrics = self.calculate_metrics(business, start_date, end_date)
            trends = self.get_revenue_trends(business, start_date, end_date)
            class_revenue = self.get_class_revenue(business, start_date, end_date)

            data = {
                'business_id': business.businessId, # Include business context in response
                'business_name': business.businessName,
                'metrics': metrics,
                'revenue_trends': trends,
                'class_revenue': class_revenue
            }

            return Response(data, status=status.HTTP_200_OK)

        except ValidationError as e:
             logger.warning(f"Validation error in RevenueAnalyticsView for {request.user.email}, Business {business.businessId}: {e.detail}")
             return Response({'error': e.detail}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as e: # Catch potential PermissionDenied from helpers (though check is now in get/post)
             logger.warning(f"Permission denied for {request.user.email} in RevenueAnalyticsView: {e}")
             return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except Exception as e:
            logger.error(f"Error fetching revenue analytics for user {request.user.email}, Business {business.businessId}: {str(e)}", exc_info=True)
            return Response(
                {'error': 'An unexpected error occurred while fetching revenue data.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


    def post(self, request):
        """Handle POST request for revenue report export."""
        user = request.user
        business = self.get_business(user) # Find the business context

        if not business:
            raise PermissionDenied("You are not associated with a business.")

        # Permission Check: Can user export data for THEIR business?
        if not user.has_perm('quickstart.export_business_revenue_data'):
            raise PermissionDenied("You do not have permission to export revenue data for this business.")

        try:
            start_date, end_date = self.get_date_range(request)

            response = HttpResponse(content_type='text/csv')
            # Format filename safely
            filename = f"{business.businessName.replace(' ', '_')}_revenue_report_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.csv"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'

            writer = csv.writer(response)

            # --- Write Header Info ---
            writer.writerow(['Revenue Report'])
            writer.writerow(['Business:', business.businessName])
            writer.writerow(['Period:', f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"])
            writer.writerow([]) # Blank line

            # --- Write Metrics (Calculated for the specific business) ---
            writer.writerow(['Key Metrics'])
            metrics = self.calculate_metrics(business, start_date, end_date)
            writer.writerow(['Metric', 'Value'])
            writer.writerow(['Total Revenue', f"${metrics['total_revenue']:.2f}"])
            writer.writerow(['Average Order Value', f"${metrics['average_order_value']:.2f}"])
            writer.writerow(['Revenue Per User', f"${metrics['revenue_per_user']:.2f}"])
            writer.writerow(['Revenue Growth (%)', f"{metrics['revenue_growth']:.1f}%"])
            writer.writerow(['Recurring Revenue', f"${metrics['recurring_revenue']:.2f}"]) # Example metric
            writer.writerow([]) # Blank line

            # --- Write Daily/Monthly Revenue Trends (Calculated for the specific business) ---
            trends = self.get_revenue_trends(business, start_date, end_date)
            trend_title = 'Daily Revenue' if (end_date.date() - start_date.date()).days <= 90 else 'Monthly Revenue'
            writer.writerow([trend_title])
            writer.writerow(['Date', 'Revenue'])
            for entry in trends:
                writer.writerow([entry['date'], f"${entry['revenue']:.2f}"])
            writer.writerow([]) # Blank line

            # --- Write Class Revenue Breakdown (Calculated for the specific business) ---
            class_revenue = self.get_class_revenue(business, start_date, end_date)
            writer.writerow(['Revenue by Class'])
            writer.writerow(['Class Name', 'Revenue'])
            for entry in class_revenue:
                # Handle potential indentation for options if implemented later
                prefix = '  • ' if entry.get('type') == 'option' else ''
                writer.writerow([f"{prefix}{entry['name']}", f"${entry['revenue']:.2f}"])

            logger.info(f"Revenue report exported for business '{business.businessName}' (ID: {business.businessId}) by user {request.user.email}")
            return response

        except ValidationError as e:
             logger.warning(f"Validation error exporting revenue report for {request.user.email}, Business {business.businessId}: {e.detail}")
             return Response({'error': e.detail}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as e:
             logger.warning(f"Permission denied exporting revenue report for {request.user.email}, Business {business.businessId}: {e}")
             return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except Exception as e:
            logger.error(f"Error exporting revenue report for user {request.user.email}, Business {business.businessId}: {str(e)}", exc_info=True)
            return Response(
                {'error': 'An unexpected error occurred while exporting the report.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )