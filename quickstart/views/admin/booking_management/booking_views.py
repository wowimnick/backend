from decimal import Decimal
from rest_framework import viewsets, status, filters 
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission # Added BasePermission
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count, DecimalField
from django.db.models.functions import Coalesce 
from datetime import timedelta

from ....models import AuditLog, Booking, ScheduleInstance, Payment, CustomUser 
from ....serializers.admin.booking_management.payment_serializers import AdminBookingListSerializer, AdminBookingPaymentSerializer
from ....serializers import BookingDetailSerializer 
from ..user_management.user_admin_views import user_can_manage # Import hierarchy helper

import logging
import csv
from django.http import HttpResponse
import stripe 
from django.conf import settings 

logger = logging.getLogger(__name__)

# --- Custom Permission Classes ---

class CanAccessBookingAdmin(BasePermission):
    message = "You do not have permission to access booking administration."
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated or not request.user.is_active: return False
        return request.user.has_perm('quickstart.access_booking_admin')

class CanManageTargetBooking(BasePermission):
    """ Checks if the user can manage the booking based on the booked user's hierarchy """
    message = "You cannot manage this booking due to hierarchy restrictions."
    def has_object_permission(self, request, view, obj):
        # obj is the Booking instance
        if not request.user or not request.user.is_authenticated or not request.user.is_active: return False
        # Check hierarchy against the user who made the booking
        return user_can_manage(request.user, obj.user)

# --- ViewSet ---

class AdminBookingViewSet(viewsets.ModelViewSet):
    """Admin-only viewset for managing bookings"""
    permission_classes = [IsAuthenticated, CanAccessBookingAdmin] # Base permission
    # Allow GET, POST (for cancel), PATCH (if allowing admin edits), DELETE (if allowed)
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [ # Added distinct related fields
        'user__first_name', 'user__last_name', 'user__email',
        'schedule_instance__schedule__option__classId__title',
        'schedule_instance__schedule__option__classId__businessId__businessName',
        'payments__stripe_payment_intent_id', # Search associated payment
        'id', # Search by Booking ID
    ]
    ordering_fields = [ # Map frontend keys to actual fields
        'user_name', # Custom handled below
        'class_name', # Custom handled below
        'date', # Custom handled below
        'booking_date', 'status', 'payment_status', 'amount_paid'
    ]
    ordering = ['-booking_date'] 

    def get_serializer_class(self):
        if self.action == 'list':
            return AdminBookingListSerializer
        # Use a detailed serializer for retrieve, potentially different for update
        return BookingDetailSerializer # Or a specific AdminBookingDetailSerializer

    def get_queryset(self):
        # Base permission check
        if not self.request.user.has_perm('quickstart.view_booking'):
            logger.warning(f"User {self.request.user.email} denied access to list bookings (missing view_booking perm).")
            return Booking.objects.none()

        # Use select/prefetch related for efficiency
        queryset = Booking.objects.select_related(
            'schedule_instance__schedule__option__classId__businessId',
            'user',
            'user__role', # For hierarchy checks if needed later
        ).prefetch_related(
            'payments' # For accessing payment info efficiently
        ).distinct() # Use distinct because of potential joins (esp. with payments)

        # --- Filtering Logic ---
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        payment_status_filter = self.request.query_params.get('payment_status')
        if payment_status_filter:
            queryset = queryset.filter(payment_status=payment_status_filter)

        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        # Filter on schedule instance date
        if start_date and end_date:
            try:
                 start_dt = timezone.datetime.strptime(start_date, '%Y-%m-%d').date()
                 end_dt = timezone.datetime.strptime(end_date, '%Y-%m-%d').date()
                 queryset = queryset.filter(schedule_instance__date__range=[start_dt, end_dt])
            except ValueError:
                 logger.warning(f"Invalid date format for booking filter: start={start_date}, end={end_date}")


        # Search and Ordering are handled by filter backends
        # Custom ordering logic needed for related fields not directly on Booking model
        ordering = self.request.query_params.get('ordering', self.ordering[0]) # Get requested ordering or default

        if ordering == 'user_name':
            queryset = queryset.order_by('user__first_name', 'user__last_name')
        elif ordering == '-user_name':
            queryset = queryset.order_by('-user__first_name', '-user__last_name')
        elif ordering == 'class_name':
            queryset = queryset.order_by('schedule_instance__schedule__option__classId__title')
        elif ordering == '-class_name':
            queryset = queryset.order_by('-schedule_instance__schedule__option__classId__title')
        elif ordering == 'date':
            queryset = queryset.order_by('schedule_instance__date', 'schedule_instance__time')
        elif ordering == '-date':
            queryset = queryset.order_by('-schedule_instance__date', '-schedule_instance__time')

        return queryset

    # --- Standard Actions Overridden ---
    def list(self, request, *args, **kwargs):
        # Permission already checked in get_queryset
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """Enhanced retrieve method that includes payment information"""
        # Base view permission check
        if not request.user.has_perm('quickstart.view_booking'):
             self.permission_denied(request, message="You cannot view booking details.")

        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data

        # Add payment information efficiently using prefetch_related
        payment = instance.payments.first() # Get from prefetched data
        if payment:
            # Use the specific serializer defined for embedding
            data['payment'] = AdminBookingPaymentSerializer(payment).data

        return Response(data)

    def partial_update(self, request, *args, **kwargs):
        """ Allow admin to update certain booking fields if permitted """
        if not request.user.has_perm('quickstart.change_booking'): # Default change perm
            self.permission_denied(request, message="You do not have permission to update bookings.")

        instance = self.get_object()
        # Hierarchy check
        if not user_can_manage(request.user, instance.user):
            self.permission_denied(request, message="You cannot manage this booking due to hierarchy restrictions.")

        # Define which fields admin can change, e.g., notes, attendance status?
        allowed_fields = ['notes', 'attendance_marked', 'attended'] # Example
        update_data = {k: v for k, v in request.data.items() if k in allowed_fields}

        if not update_data:
            return Response({"detail": "No valid fields provided for update."}, status=status.HTTP_400_BAD_REQUEST)

        # Log action before saving
        logger.info(f"Booking ID {instance.pk} being updated by Admin {request.user.email}. Changes: {update_data}")
        # Optionally add to AuditLog

        serializer = self.get_serializer(instance, data=update_data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return Response(serializer.data)


    def destroy(self, request, *args, **kwargs):
        """ Allow admin to delete bookings if permitted """
        if not request.user.has_perm('quickstart.delete_booking'):
            self.permission_denied(request, message="You do not have permission to delete bookings.")

        instance = self.get_object()
        # Hierarchy check
        if not user_can_manage(request.user, instance.user):
            self.permission_denied(request, message="You cannot delete this booking due to hierarchy restrictions.")

        logger.warning(f"Booking ID {instance.pk} deleted by Admin {request.user.email}")
        # Add to AuditLog if needed
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanAccessBookingAdmin])
    def cancel(self, request, pk=None):
        """Admin cancel booking action (Updates booking status ONLY - No hierarchy check)"""
        # Check specific cancel permission
        if not request.user.has_perm('quickstart.cancel_any_booking'):
             self.permission_denied(request, message="You do not have permission to cancel this booking.")

        booking = self.get_object()

        if booking.status not in ['confirmed', 'pending']: # Check if cancellable
            return Response(
                {'error': f'Booking with status "{booking.status}" cannot be cancelled.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        reason = request.data.get('reason', 'Cancelled by administrator')

        try:
            with transaction.atomic():
                # Update booking status ONLY
                booking.status = 'cancelled'
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = reason
                booking.save(update_fields=['status', 'cancelled_at', 'cancellation_reason'])

            # Log the cancellation
            log_details = f"Booking cancelled by admin. Reason: {reason}. Refund must be processed separately if applicable."
            self._log_booking_action(booking, 'booking_cancel_admin', log_details, request)

        except Exception as e:
            logger.error(f"Error cancelling Booking {pk}: {str(e)}", exc_info=True)
            return Response(
                {'error': f"Failed to cancel booking. {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Check if a potentially refundable payment exists to inform the UI
        payment = booking.payments.filter(status__in=['succeeded', 'partially_refunded']).order_by('-created_at').first()
        refundable_payment_exists = payment is not None and payment.available_refund_amount > 0

        # Return updated booking data
        serializer = self.get_serializer(booking)
        data = serializer.data
        # Include current payment status for info
        if payment:
             data['payment'] = AdminBookingPaymentSerializer(payment).data
        # Add flag for UI
        data['refundable_payment_exists'] = refundable_payment_exists
        if refundable_payment_exists:
             data['payment_intent_id_for_refund'] = payment.stripe_payment_intent_id
             data['payment_pk_for_refund'] = payment.pk # Provide payment PK too

        return Response(data)

    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """Get booking analytics for admin dashboard"""
        logger.info(f"Analytics endpoint hit. Request query params: {request.query_params}") # Log incoming params

        if not request.user.has_perm('quickstart.view_booking_analytics'):
            self.permission_denied(request, message="You cannot view booking analytics.")

        try:
            # --- Date Range Setup ---
            # Use timezone.localdate() for date-only comparisons
            today = timezone.localdate()
            default_end_date = today
            # Default start date is 30 days *before* today
            default_start_date = today - timedelta(days=30)

            start_param = request.query_params.get('start_date')
            end_param = request.query_params.get('end_date')

            start_date_dt = default_start_date
            end_date_dt = default_end_date

            if start_param and end_param:
                try:
                     # Convert query params directly to date objects
                     start_date_dt = timezone.datetime.strptime(start_param, '%Y-%m-%d').date()
                     end_date_dt = timezone.datetime.strptime(end_param, '%Y-%m-%d').date()
                     logger.info(f"Analytics using date range from params: START={start_date_dt} END={end_date_dt}")
                except ValueError:
                     logger.warning(f"Invalid date format in analytics params: start='{start_param}', end='{end_param}'. Using default range.")
                     # Fallback to defaults if parsing fails
                     start_date_dt = default_start_date
                     end_date_dt = default_end_date
            else:
                 logger.info(f"Analytics using default date range: START={start_date_dt} END={end_date_dt}")


            # --- **** CRITICAL CHECK: Base Queryset Filtering **** ---
            # Filter by schedule_instance__date within the calculated range (inclusive)
            # Ensure schedule_instance is not null if filtering on it
            bookings_in_period = Booking.objects.filter(
                schedule_instance__isnull=False, # Add this check!
                schedule_instance__date__gte=start_date_dt,
                schedule_instance__date__lte=end_date_dt
            )
            # Log the count *after* filtering
            count_in_period = bookings_in_period.count()
            logger.info(f"Bookings query for period [{start_date_dt} - {end_date_dt}] found {count_in_period} bookings.")
            # Optional: Log the actual SQL query for deeper debugging if needed
            # logger.debug(f"Analytics Query SQL: {bookings_in_period.query}")


            # --- Aggregations ---
            # Perform aggregations on the *already filtered* queryset
            aggregates = bookings_in_period.aggregate(
                total_bookings_in_period=Count('id'), # Count should match count_in_period
                confirmed_bookings=Count('id', filter=Q(status='confirmed')),
                completed_bookings=Count('id', filter=Q(status='completed')),
                cancelled_bookings=Count('id', filter=Q(status='cancelled')),
                # Revenue: Sum amount_paid for COMPLETED bookings in the period
                total_revenue=Coalesce(
                    Sum('amount_paid', filter=Q(status='completed')),
                    Decimal(0),
                    output_field=DecimalField(max_digits=10, decimal_places=2) # Ensure output_field precision
                )
            )
            logger.info(f"Aggregates calculated for period: {aggregates}")


            # --- Calculation based on aggregates ---
            total_bookings = aggregates['total_bookings_in_period'] # Use the count specific to the period
            completed_bookings_count = aggregates['completed_bookings']
            cancellation_rate = (aggregates['cancelled_bookings'] / total_bookings * 100) if total_bookings > 0 else 0
            avg_booking_value = (aggregates['total_revenue'] / completed_bookings_count) if completed_bookings_count > 0 else Decimal(0)


            # --- Calculate Growth (Compare previous period) ---
            previous_period_duration = end_date_dt - start_date_dt
            # Ensure previous period doesn't overlap
            previous_start_date_dt = start_date_dt - (previous_period_duration + timedelta(days=1))
            previous_end_date_dt = start_date_dt - timedelta(days=1)

            # Filter previous period correctly by schedule_instance__date
            previous_total_bookings = Booking.objects.filter(
                schedule_instance__isnull=False, # Add this check!
                schedule_instance__date__gte=previous_start_date_dt,
                schedule_instance__date__lte=previous_end_date_dt
            ).count()
            logger.info(f"Previous period [{previous_start_date_dt} - {previous_end_date_dt}] total bookings: {previous_total_bookings}")

            booking_growth = 0
            if previous_total_bookings > 0:
                booking_growth = ((total_bookings - previous_total_bookings) / previous_total_bookings) * 100


            # --- Prepare Response Data ---
            response_data = {
                'total_bookings': total_bookings,
                'confirmed_bookings': aggregates['confirmed_bookings'],
                'completed_bookings': completed_bookings_count,
                'cancelled_bookings': aggregates['cancelled_bookings'],
                'cancellation_rate': round(cancellation_rate, 1),
                'total_revenue': float(aggregates['total_revenue']), # Convert Decimal to float
                'average_booking_value': float(avg_booking_value), # Convert Decimal to float
                'booking_growth': round(booking_growth, 1),
                'start_date': start_date_dt.strftime('%Y-%m-%d'), # Send back the dates used
                'end_date': end_date_dt.strftime('%Y-%m-%d')
            }
            logger.info(f"Returning analytics data: {response_data}")

            return Response(response_data)

        except Exception as e:
            logger.error(f"Critical error in booking analytics: {str(e)}", exc_info=True) # Log traceback
            return Response(
                {'error': 'Failed to retrieve booking analytics'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export bookings data to CSV"""
        if not request.user.has_perm('quickstart.export_booking_data'):
            self.permission_denied(request, message="You cannot export booking data.")

        try:
            queryset = self.filter_queryset(self.get_queryset()) # Apply filters

            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="bookings_export.csv"'
            writer = csv.writer(response)

            # Write header
            writer.writerow([
                'Booking ID', 'Student Name', 'Student Email', 'Class',
                'Business', 'Session Date', 'Session Time', 'Status', 'Payment Status',
                'Amount Paid', 'Participants', 'Booking Date', 'Payment Intent ID'
            ])

            # Write data efficiently
            for booking in queryset.iterator(): # Use iterator for large datasets
                payment_id = booking.payments.first().stripe_payment_intent_id if booking.payments.exists() else ""
                writer.writerow([
                    booking.id,
                    f"{booking.user.first_name} {booking.user.last_name}".strip(),
                    booking.user.email,
                    booking.schedule_instance.schedule.option.classId.title if booking.schedule_instance else 'N/A',
                    booking.schedule_instance.schedule.option.classId.businessId.businessName if booking.schedule_instance else 'N/A',
                    booking.schedule_instance.date if booking.schedule_instance else 'N/A',
                    booking.schedule_instance.time if booking.schedule_instance else 'N/A',
                    booking.status,
                    booking.payment_status,
                    booking.amount_paid,
                    booking.participants,
                    booking.booking_date.strftime('%Y-%m-%d %H:%M:%S'),
                    payment_id
                ])

            return response

        except Exception as e:
            logger.error(f"Error exporting bookings: {str(e)}", exc_info=True)
            return HttpResponse(f"Error exporting booking data: {str(e)}", status=500, content_type="text/plain")

    def _log_booking_action(self, booking, action_code, details, request):
        """ Helper to log booking related actions """
        try:
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action=action_code,
                details=details,
                target_user=booking.user,
                target_model='Booking',
                target_id=str(booking.id),
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                metadata={ # Add relevant booking metadata
                    'class_id': booking.schedule_instance.schedule.option.classId_id if booking.schedule_instance else None,
                    'instance_id': booking.schedule_instance_id if booking.schedule_instance else None,
                    'status': booking.status,
                }
            )
        except Exception as e:
             logger.error(f"Failed to create audit log for booking action {action_code}: {str(e)}", exc_info=True)