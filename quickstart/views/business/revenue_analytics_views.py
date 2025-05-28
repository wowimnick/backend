from decimal import Decimal
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from django.db.models import (
    Sum, Count, F, ExpressionWrapper, FloatField, DecimalField,
    Q, Value, Case, When, IntegerField, Subquery, OuterRef, Min, DateTimeField, DateField # Added DateTimeField, DateField
)
from django.db.models.functions import (
    TruncDate, ExtractMonth, ExtractYear, Coalesce, TruncHour # Added TruncHour
)
from django.utils import timezone
from datetime import datetime, timedelta, date as datetime_date
from rest_framework.exceptions import ValidationError, PermissionDenied
import csv
from django.http import HttpResponse
import logging
import pytz

from quickstart.models import Booking, BusinessInfo, ClassOption, ClassesMain, CustomUser

logger = logging.getLogger(__name__)

class RevenueAnalyticsView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_business(self, user):
        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(managers=user)
        ).first()
        return business

    def get_date_range(self, request):
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        try:
            if start_date_str and end_date_str:
                start_date_naive = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                end_date_naive = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            else:
                end_date_naive = timezone.localdate()
                start_date_naive = end_date_naive - timedelta(days=29)
            if start_date_naive > end_date_naive:
                raise ValidationError("Start date cannot be after end date.")
            start_datetime_aware = timezone.make_aware(datetime.combine(start_date_naive, datetime.min.time()), pytz.utc)
            end_datetime_aware = timezone.make_aware(datetime.combine(end_date_naive, datetime.max.time()), pytz.utc)
            return start_datetime_aware, end_datetime_aware
        except ValueError: raise ValidationError("Invalid date format. Please use YYYY-MM-DD.")
        except Exception as e: logger.error(f"Error processing date range: {e}", exc_info=True); raise ValidationError("Error processing date range.")

    def get_valid_bookings_queryset(self, business, start_date, end_date, class_id=None):
        if not business: return Booking.objects.none()
        base_bookings = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            booking_date__range=[start_date, end_date],
            payment_status='paid'
        ).select_related('schedule_instance__schedule__option__classId', 'schedule_instance__schedule__option', 'user')

        if class_id: # Apply class filter if provided
            base_bookings = base_bookings.filter(schedule_instance__schedule__option__classId_id=class_id)

        first_course_booking_ids = Subquery(
            Booking.objects.filter(
                booking_group_id=OuterRef('booking_group_id'),
                schedule_instance__schedule__option__classId__businessId=business, # Ensure subquery is also scoped
                booking_date__range=[start_date, end_date], payment_status='paid'
            ).order_by('booking_date', 'id').values('id')[:1]
        )
        valid_bookings_qs = base_bookings.filter(Q(booking_group_id__isnull=True) | Q(id=first_course_booking_ids))
        return valid_bookings_qs

    def calculate_metrics(self, business, start_date, end_date, class_id=None):
        current_period_qs = self.get_valid_bookings_queryset(business, start_date, end_date, class_id)
        period_length_timedelta = (end_date - start_date);
        if period_length_timedelta < timedelta(days=1): period_length_timedelta = timedelta(days=1)
        previous_end_date = start_date - timedelta.resolution
        previous_start_date = previous_end_date - period_length_timedelta + timedelta.resolution
        previous_period_qs = self.get_valid_bookings_queryset(business, previous_start_date, previous_end_date, class_id)

        current_aggregates = current_period_qs.aggregate(
            total_gross_revenue_decimal=Coalesce(Sum('amount_paid'), Value(Decimal('0.0')), output_field=DecimalField()),
            total_bookings=Count('id'),
            unique_users=Count('user', distinct=True),
            total_participant_spots_decimal=Coalesce(Sum('participants'), Value(0), output_field=IntegerField())
        )
        current_total_gross_revenue = float(current_aggregates['total_gross_revenue_decimal'])
        current_total_bookings = current_aggregates['total_bookings']
        current_unique_users = current_aggregates['unique_users']
        current_total_participant_spots = int(current_aggregates['total_participant_spots_decimal'])


        previous_aggregates = previous_period_qs.aggregate(
            prev_gross_revenue_decimal=Coalesce(Sum('amount_paid'), Value(Decimal('0.0')), output_field=DecimalField())
        )
        previous_total_gross_revenue = float(previous_aggregates['prev_gross_revenue_decimal'])

        average_order_value = (current_total_gross_revenue / current_total_bookings) if current_total_bookings > 0 else 0.0
        revenue_per_user = (current_total_gross_revenue / current_unique_users) if current_unique_users > 0 else 0.0
        revenue_growth = 0.0
        if previous_total_gross_revenue > 0: revenue_growth = ((current_total_gross_revenue - previous_total_gross_revenue) / previous_total_gross_revenue) * 100.0
        elif current_total_gross_revenue > 0 and previous_total_gross_revenue == 0: revenue_growth = 100.0
        
        platform_fee_rate = Decimal('0.13') # 13%
        estimated_platform_fees = float(current_aggregates['total_gross_revenue_decimal'] * platform_fee_rate)
        estimated_net_revenue = float(current_aggregates['total_gross_revenue_decimal'] * (Decimal('1.0') - platform_fee_rate))
        revenue_per_spot = (current_total_gross_revenue / current_total_participant_spots) if current_total_participant_spots > 0 else 0.0

        return {
            'total_gross_revenue': round(current_total_gross_revenue, 2),
            'estimated_platform_fees': round(estimated_platform_fees, 2),
            'estimated_net_revenue': round(estimated_net_revenue, 2),
            'average_order_value': round(average_order_value, 2),
            'revenue_per_user': round(revenue_per_user, 2),
            'revenue_growth': round(revenue_growth, 1),
            'revenue_per_spot': round(revenue_per_spot, 2),
            'recurring_revenue': 0.0, # Placeholder
        }

    def get_revenue_trends(self, business, start_date, end_date, class_id=None):
        valid_bookings_qs = self.get_valid_bookings_queryset(business, start_date, end_date, class_id)
        business_pytz = pytz.timezone(business.business_timezone)

        trends_qs = valid_bookings_qs.annotate(
            # Convert UTC booking_date to business's local date for grouping
            local_booking_date_trunc=TruncDate(F('booking_date'), tzinfo=business_pytz)
        ).values('local_booking_date_trunc').annotate(
            gross_revenue_decimal=Coalesce(Sum('amount_paid'), Value(Decimal('0.0')), output_field=DecimalField())
        ).order_by('local_booking_date_trunc')
        
        all_dates_in_range_local = {}
        current_scan_local_date = start_date.astimezone(business_pytz).date()
        end_scan_local_date = end_date.astimezone(business_pytz).date()
        while current_scan_local_date <= end_scan_local_date:
            all_dates_in_range_local[current_scan_local_date.isoformat()] = {'gross_revenue': 0.0, 'net_revenue': 0.0, 'platform_fees': 0.0}
            current_scan_local_date += timedelta(days=1)

        platform_fee_rate = Decimal('0.13')
        for entry in trends_qs:
            date_iso = entry['local_booking_date_trunc'].isoformat()
            gross_rev = entry['gross_revenue_decimal']
            if date_iso in all_dates_in_range_local:
                all_dates_in_range_local[date_iso]['gross_revenue'] = float(gross_rev)
                all_dates_in_range_local[date_iso]['platform_fees'] = float(gross_rev * platform_fee_rate)
                all_dates_in_range_local[date_iso]['net_revenue'] = float(gross_rev * (Decimal('1.0') - platform_fee_rate))
        
        formatted_trends = [
            {'date': date_str, **rev_data}
            for date_str, rev_data in sorted(all_dates_in_range_local.items())
        ]
        return formatted_trends


    def get_class_revenue(self, business, start_date, end_date, class_id_filter=None): # Renamed class_id
        valid_bookings_qs = self.get_valid_bookings_queryset(business, start_date, end_date, class_id_filter)
        class_revenue_data = valid_bookings_qs.values(
            'schedule_instance__schedule__option__classId'
        ).annotate(
            total_gross_revenue_decimal=Coalesce(Sum('amount_paid'), Value(Decimal('0.0')), output_field=DecimalField())
        ).order_by('-total_gross_revenue_decimal')

        class_ids = [item['schedule_instance__schedule__option__classId'] for item in class_revenue_data if item['schedule_instance__schedule__option__classId'] is not None]
        class_titles_map = dict(ClassesMain.objects.filter(classId__in=class_ids).values_list('classId', 'title'))

        platform_fee_rate = Decimal('0.13')
        result = []
        for item in class_revenue_data:
             class_id = item['schedule_instance__schedule__option__classId']
             if class_id:
                 gross_rev = item['total_gross_revenue_decimal']
                 result.append({
                     'id': class_id,
                     'name': class_titles_map.get(class_id, f"Class ID {class_id}"),
                     'gross_revenue': float(gross_rev),
                     'platform_fees': float(gross_rev * platform_fee_rate),
                     'net_revenue': float(gross_rev * (Decimal('1.0') - platform_fee_rate)),
                     'type': 'class' # For frontend differentiation if needed
                 })
        return result
    
    # Placeholder for Revenue by Booking Type (Single Session vs Full Course)
    def get_revenue_by_booking_type(self, business, start_date, end_date, class_id=None):
        # This method is a placeholder and would need real implementation
        # if "Recurring Revenue" or distinct "Revenue by Booking Type" charts are desired.
        # For now, it returns a structure indicating it's under construction.
        return [
            {"type": "Single Session Revenue", "revenue": "Under Construction"},
            {"type": "Full Course Revenue", "revenue": "Under Construction"}
        ]


    def get(self, request):
        user = request.user
        business = self.get_business(user)
        if not business: raise PermissionDenied("You are not associated with a business.")
        if not user.has_perm('quickstart.view_business_revenue_analytics'):
            raise PermissionDenied("You do not have permission to view revenue analytics.")

        try:
            start_date_utc, end_date_utc = self.get_date_range(request)
            class_id_filter = request.query_params.get('class_id')
            if class_id_filter and not class_id_filter.isdigit(): # Basic validation
                raise ValidationError("Invalid class_id format.")
            class_id_filter = int(class_id_filter) if class_id_filter else None


            metrics = self.calculate_metrics(business, start_date_utc, end_date_utc, class_id_filter)
            trends = self.get_revenue_trends(business, start_date_utc, end_date_utc, class_id_filter)
            class_revenue_breakdown = self.get_class_revenue(business, start_date_utc, end_date_utc, class_id_filter)
            revenue_by_booking_type = self.get_revenue_by_booking_type(business, start_date_utc, end_date_utc, class_id_filter)


            data = {
                'business_id': business.businessId,
                'business_name': business.businessName,
                'metrics': metrics,
                'revenue_trends': trends, # Now includes gross, net, fees per day/month
                'class_revenue': class_revenue_breakdown, # Now includes gross, net, fees per class
                'revenue_by_booking_type': revenue_by_booking_type # Placeholder
            }
            return Response(data, status=status.HTTP_200_OK)
        except ValidationError as e: return Response({'error': e.detail}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as e: return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except Exception as e:
            logger.error(f"Error fetching revenue analytics for Business {business.businessId if business else 'N/A'}: {str(e)}", exc_info=True)
            return Response({'error': 'An unexpected error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request): # Export to CSV
        user = request.user
        business = self.get_business(user)
        if not business: raise PermissionDenied("Not associated with a business.")
        if not user.has_perm('quickstart.export_business_revenue_data'):
            raise PermissionDenied("No permission to export revenue data.")

        try:
            start_date_utc, end_date_utc = self.get_date_range(request)
            class_id_filter = request.query_params.get('class_id')
            if class_id_filter and not class_id_filter.isdigit(): class_id_filter = None # Ignore invalid

            response = HttpResponse(content_type='text/csv')
            filename = f"{business.businessName.replace(' ', '_')}_revenue_report_{start_date_utc.strftime('%Y%m%d')}_{end_date_utc.strftime('%Y%m%d')}.csv"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            writer = csv.writer(response)

            writer.writerow(['Revenue Report']); writer.writerow(['Business:', business.businessName])
            writer.writerow(['Period:', f"{start_date_utc.date().strftime('%Y-%m-%d')} to {end_date_utc.date().strftime('%Y-%m-%d')}"])
            if class_id_filter:
                try:
                    filtered_class_name = ClassesMain.objects.get(pk=class_id_filter, businessId=business).title
                    writer.writerow(['Filtered by Class:', filtered_class_name])
                except ClassesMain.DoesNotExist:
                    writer.writerow(['Filtered by Class ID:', class_id_filter, '(Name not found)'])
            writer.writerow([])

            metrics = self.calculate_metrics(business, start_date_utc, end_date_utc, class_id_filter)
            writer.writerow(['Key Metrics', 'Value']);
            writer.writerow(['Total Gross Revenue', f"${metrics['total_gross_revenue']:.2f}"])
            writer.writerow(['Estimated Platform Fees (13%)', f"${metrics['estimated_platform_fees']:.2f}"])
            writer.writerow(['Estimated Net Revenue', f"${metrics['estimated_net_revenue']:.2f}"])
            writer.writerow(['Average Order Value', f"${metrics['average_order_value']:.2f}"])
            writer.writerow(['Revenue Per User', f"${metrics['revenue_per_user']:.2f}"])
            writer.writerow(['Revenue Growth (%)', f"{metrics['revenue_growth']:.1f}%"])
            writer.writerow(['Revenue Per Participant Spot', f"${metrics['revenue_per_spot']:.2f}"])
            writer.writerow([]);

            trends = self.get_revenue_trends(business, start_date_utc, end_date_utc, class_id_filter)
            trend_title = 'Daily Revenue Detail' # Since trends now include net/fees
            writer.writerow([trend_title]); writer.writerow(['Date (Local Business Time)', 'Gross Revenue', 'Platform Fees', 'Net Revenue'])
            for entry in trends:
                writer.writerow([entry['date'], f"${entry['gross_revenue']:.2f}", f"${entry['platform_fees']:.2f}", f"${entry['net_revenue']:.2f}"])
            writer.writerow([]);

            class_revenue = self.get_class_revenue(business, start_date_utc, end_date_utc, class_id_filter)
            writer.writerow(['Revenue by Class']); writer.writerow(['Class Name', 'Gross Revenue', 'Platform Fees', 'Net Revenue'])
            for entry in class_revenue:
                writer.writerow([entry['name'], f"${entry['gross_revenue']:.2f}", f"${entry['platform_fees']:.2f}", f"${entry['net_revenue']:.2f}"])
            
            logger.info(f"Revenue report exported for Business '{business.businessName}' by {user.email}")
            return response
        except ValidationError as e: return Response({'error': e.detail}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as e: return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except Exception as e:
            logger.error(f"Error exporting revenue report for Business {business.businessId if business else 'N/A'}: {str(e)}", exc_info=True)
            return Response({'error': 'Error exporting report.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)