# views/business/business_booking_views.py
from decimal import Decimal
from django.db import models, transaction
from django.db.models import Q, Sum, Count, Avg, F, Prefetch, Window, Value, FloatField, ExpressionWrapper, Subquery, OuterRef, IntegerField
from django.db.models.functions import TruncDate, ExtractWeekDay, datetime, Concat, RowNumber, Cast, ExtractHour, Coalesce
from django.utils import timezone
from datetime import datetime, timedelta
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.pagination import PageNumberPagination

# Adjust import paths as needed
from ...models import BusinessInfo, Booking, ScheduleInstance, CustomUser
from ...serializers import (
    BookingDetailSerializer, # Generic detail
    BusinessBookingListSerializer, # Specific list for business
)
# Import specific business permissions
from ...utils.permissions import CanManageOwnClasses # For checking if user can manage related class

import logging
logger = logging.getLogger(__name__)

from ...utils.email_utils import send_booking_cancelled_by_other_email

# --- Permissions ---
class CanViewOwnBusinessBookings(BasePermission):
    """ Allows access if user has 'view_own_business_bookings' perm AND owns/manages the related business."""
    message = "You do not have permission to view bookings for this business."
    def has_permission(self, request, view):
         user = request.user
         if not user or not user.is_authenticated: return False
         # Check base permission
         has_base_perm = user.has_perm('quickstart.view_own_business_bookings')
         # Check if user is associated with *any* business
         has_business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).exists()
         return has_base_perm and has_business

    def has_object_permission(self, request, view, obj):
        # obj is the Booking instance
        user = request.user
        # Find the business context for the user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business: return False # User has perm but no business context (shouldn't happen if has_permission checks)

        # Check if the booking's class belongs to the user's business
        try:
            return obj.schedule_instance.schedule.option.classId.businessId == business
        except AttributeError:
            return False # Handle cases where relations might be missing

class CanManageOwnBusinessBookings(BasePermission):
     """ Allows actions like cancelling or marking attendance if user manages the business."""
     message = "You do not have permission to manage this booking."
     def has_permission(self, request, view):
         user = request.user
         if not user or not user.is_authenticated: return False
         # Check required permissions for management actions
         # Requires ability to view and specific action permission
         return (
             user.has_perm('quickstart.view_own_business_bookings') and
             (user.has_perm('quickstart.cancel_business_booking') or
              user.has_perm('quickstart.mark_booking_attendance')) and # Check if they have *either* manage perm
             BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).exists()
         )

     def has_object_permission(self, request, view, obj):
        # obj is the Booking instance
        user = request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business: return False

        # Check if the booking's class belongs to the user's business
        try:
            return obj.schedule_instance.schedule.option.classId.businessId == business
        except AttributeError:
            return False

# --- Pagination ---
class BusinessBookingPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100

# --- Business ViewSet ---
class BusinessBookingViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for Business Users to view and manage bookings within their own business.
    Provides listing, retrieval, analytics, cancellation, and attendance marking.
    """
    serializer_class = BusinessBookingListSerializer # Default for list
    permission_classes = [IsAuthenticated, CanViewOwnBusinessBookings] # Base permission for viewing
    pagination_class = BusinessBookingPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [ # Fields relevant for business searching
        'user__email', 'user__first_name', 'user__last_name',
        'schedule_instance__schedule__option__classId__title', # Class name
        'schedule_instance__schedule__option__title', # Option name
        'id' # Booking ID
    ]
    ordering_fields = [
        'schedule_instance__date', 'booking_date', 'status',
        'user__first_name', 'user__last_name',
        'schedule_instance__schedule__option__classId__title'
    ]
    ordering = ['-schedule_instance__date', '-schedule_instance__time']

    # Only allow GET and POST (for actions)
    http_method_names = ['get', 'post', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return BookingDetailSerializer # Use generic detail
        # No serializer needed for analytics action
        # Other actions might return detail or list serializer based on outcome
        return BusinessBookingListSerializer # Default for list

    def get_business_context(self):
        """Helper to get the business associated with the request user."""
        user = self.request.user
        try:
            # Use filter().first() for safety, though owner/manager should be unique link usually
            business = BusinessInfo.objects.get(Q(owner=user) | Q(managers=user))
            return business
        except BusinessInfo.DoesNotExist:
            # This should ideally be caught by permission classes, but is a safeguard
            raise PermissionDenied("You are not associated with a business.")
        except BusinessInfo.MultipleObjectsReturned:
            # Log this serious issue - user linked to multiple businesses as owner/manager?
            logger.error(f"User {user.email} is owner/manager of multiple businesses. Ambiguous context.")
            raise PermissionDenied("Ambiguous business context. Please contact support.")


    def get_queryset(self):
        """ Filters queryset to bookings belonging to the user's associated business. """
        # Business context is established here
        business = self.get_business_context()

        # Base queryset optimized for business view
        queryset = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business
        ).select_related(
            'schedule_instance__schedule__option__classId', # Class details
            'schedule_instance__schedule__option',         # Option details
            'user',                                         # Student details
            # Add other relations needed for list/detail view
        ).prefetch_related(
             # Prefetch payments if needed
             # 'payments'
        )

        queryset = self._apply_business_filters(queryset, self.request)
        return queryset.distinct() # Distinct needed if filters join multiple times

    def _apply_business_filters(self, queryset, request):
        # Filters common for business view
        status_param = request.query_params.get('status')
        if status_param and status_param != 'all':
            status_list = [s.strip() for s in status_param.split(',') if s.strip()] # Split and clean
            if status_list:
                queryset = queryset.filter(status__in=status_list)

        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        # Filter by SCHEDULE INSTANCE date for upcoming/past classes view
        if start_date: queryset = queryset.filter(schedule_instance__date__gte=start_date)
        if end_date: queryset = queryset.filter(schedule_instance__date__lte=end_date)

        class_id = request.query_params.get('class_id')
        if class_id and class_id.isdigit():
            queryset = queryset.filter(schedule_instance__schedule__option__classId_id=class_id)

        user_id = request.query_params.get('user_id')
        if user_id and user_id.isdigit():
            queryset = queryset.filter(user_id=user_id)

        return queryset

    def list(self, request, *args, **kwargs):
        """ Lists bookings for the business associated with the current user. """
        # Permissions checked by class + get_queryset ensures business context
        queryset = self.filter_queryset(self.get_queryset()) # Apply search/ordering/filtering

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True, context={'request': request})
            response = self.get_paginated_response(serializer.data)
            # Add summary counts for the business context
            business = self.get_business_context()
            count_qs = Booking.objects.filter(schedule_instance__schedule__option__classId__businessId=business)
            count_qs = self._apply_business_filters(count_qs, request) # Apply same filters for accurate counts
            response.data['summary'] = {
                'total_confirmed': count_qs.filter(status='confirmed').count(),
                'total_completed': count_qs.filter(status='completed').count(),
                'total_cancelled': count_qs.filter(status='cancelled').count(),
                'total_bookings': count_qs.count(),
            }
            return response

        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


    def retrieve(self, request, *args, **kwargs):
        """ Retrieve a specific booking within the user's business context. """
        # get_object uses get_queryset, which filters by business.
        # CanViewOwnBusinessBookings checks permission.
        instance = self.get_object()
        serializer = self.get_serializer(instance) # Uses BookingDetailSerializer
        return Response(serializer.data)

    # --- Custom Actions for Business ---

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanManageOwnBusinessBookings])
    def mark_attendance(self, request, pk=None):
        """ Mark attendance for a specific booking within the business context. """
        # Check the specific permission required for this action
        if not request.user.has_perm('quickstart.mark_booking_attendance'):
            raise PermissionDenied("You do not have permission to mark attendance.")

        # get_object ensures the booking belongs to the user's business via CanManageOwnBusinessBookings
        booking = self.get_object()
        attended_status = request.data.get('attended') # Expect boolean true/false

        if attended_status is None or not isinstance(attended_status, bool):
            raise ValidationError({'attended': 'Boolean field "attended" (true/false) is required.'})

        # Can only mark attendance for confirmed bookings, usually after the class time
        if booking.status != 'confirmed':
             # Allow marking completed? Maybe. For now, restrict to confirmed.
             raise ValidationError({'status': 'Can only mark attendance for confirmed bookings.'})

        # Optional: Check if class time has passed
        # instance_datetime = timezone.make_aware(datetime.combine(booking.schedule_instance.date, booking.schedule_instance.time))
        # if instance_datetime > timezone.now():
        #     raise ValidationError({'time': 'Cannot mark attendance before the class starts.'})

        with transaction.atomic():
             booking.attendance_marked = True
             booking.attended = attended_status
             # Optionally update booking status to completed if attended=true
             if attended_status and booking.status == 'confirmed':
                 booking.status = 'completed'

             booking.save(update_fields=['attendance_marked', 'attended', 'status']) # Add 'status' if changed

             # Optionally update the parent ScheduleInstance's attendance_marked flag if all bookings are marked
             instance = booking.schedule_instance
             if not instance.bookings.filter(attendance_marked=False, status='confirmed').exists():
                 instance.attendance_marked = True
                 instance.save(update_fields=['attendance_marked'])

        logger.info(f"Attendance marked for Booking {pk} (Attended: {attended_status}) by business user {request.user.email}")
        serializer = BookingDetailSerializer(booking, context={'request': request}) # Return updated detail
        return Response(serializer.data)


    @action(detail=True, methods=['post'], url_path='cancel', permission_classes=[IsAuthenticated, CanManageOwnBusinessBookings])
    def business_cancel(self, request, pk=None):
        """ Allows a business user to cancel a booking within their business. """
        # Check the specific permission required
        if not request.user.has_perm('quickstart.cancel_business_booking'):
            raise PermissionDenied("You do not have permission to cancel bookings for this business.")

        # get_object ensures booking belongs to user's business
        booking = self.get_object()
        cancellation_reason = request.data.get('reason', '').strip()

        if not cancellation_reason:
            raise ValidationError({'reason': 'A reason is required for cancellation.'})

        if booking.status not in ['confirmed', 'pending']:
             raise ValidationError({'status': f'Cannot cancel a booking with status "{booking.status}".'})

        user_to_notify = booking.user # Get the user associated with the booking

        try: # Wrap the entire process in try/except
            with transaction.atomic():
                booking.status = 'cancelled'
                booking.cancelled_at = timezone.now()
                # Prepend who cancelled it for clarity
                booking.cancellation_reason = f"Cancelled by business: {cancellation_reason}"
                # Handle refund logic if applicable (mark as pending, trigger separate process)
                if booking.payment_status == 'paid':
                    booking.payment_status = 'refund_pending'
                    # TODO: Trigger async refund task here if applicable!
                    logger.info(f"Booking {pk} cancelled by business user {request.user.email}. Marked payment as refund_pending.")
                else:
                    logger.info(f"Booking {pk} cancelled by business user {request.user.email}. No refund processing needed (Payment Status: {booking.payment_status}).")

                booking.save(update_fields=['status', 'cancelled_at', 'cancellation_reason', 'payment_status'])

                try:
                    contact_info = settings.NOTIFICATION_SETTINGS.get('reply_to', 'support@classeasily.com')
                    send_booking_cancelled_by_other_email(
                        user=user_to_notify,
                        booking=booking,
                        cancelled_by="the business", # Indicate who cancelled
                        reason=cancellation_reason, # Pass the reason provided
                        contact_info=contact_info # Provide support contact
                    )
                    logger.info(f"'Cancelled by other' email prepared/queued for user {user_to_notify.email} for booking {booking.id}")
                except Exception as email_error:
                    # Log email error but don't fail the cancellation itself
                    logger.error(f"Failed to send cancellation email for booking {booking.id}: {email_error}", exc_info=True)
                # *** END ADDED ***

            # Log the cancellation (audit log - if you have one)
            # self._log_booking_action(booking, 'booking_cancel_business', ...)

            # Return updated booking details using the appropriate serializer
            serializer = BookingDetailSerializer(booking, context={'request': request}) # Or appropriate serializer
            return Response(serializer.data, status=status.HTTP_200_OK)

        except ValidationError as ve: # Catch validation errors specifically
             logger.warning(f"Validation error during business cancel for booking {pk}: {ve.detail}")
             return Response(ve.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Error during business cancellation for booking {pk}: {e}", exc_info=True)
            return Response(
                {'error': 'An error occurred while cancelling the booking.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'], permission_classes=[IsAuthenticated]) # Use specific analytics perm
    def analytics(self, request):
        """ Get comprehensive booking analytics FOR THE USER'S BUSINESS """
        # Check specific analytics permission
        if not request.user.has_perm('quickstart.view_own_booking_analytics'):
             raise PermissionDenied("You do not have permission to view booking analytics for this business.")

        # Business context established here
        business = self.get_business_context()

        try:
            start_date, end_date = self._get_date_range(request) # Use helper

            # Get base queryset filtered for THIS business
            bookings_qs = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business,
                booking_date__range=[start_date, end_date] # Use booking date for analytics period
            ).select_related(
                'schedule_instance__schedule__option__classId',
                'schedule_instance__schedule__option',
                'user'
            )

            # --- Aggregation Logic (Scoped to business_qs) ---
            # Basic counts (using all bookings in the period for these counts)
            total_aggregates = bookings_qs.aggregate(
                 total_bookings=Count('id'),
                 confirmed_count=Count('id', filter=Q(status='confirmed')),
                 completed_count=Count('id', filter=Q(status='completed')),
                 cancelled_count=Count('id', filter=Q(status='cancelled')),
                 # Calculate revenue only from completed & paid bookings
                 total_revenue=Coalesce(Sum('amount_paid', filter=Q(status='completed', payment_status='paid')), Value(Decimal('0.0')), output_field=models.DecimalField())
            )
            total_bookings = total_aggregates['total_bookings']
            cancelled_bookings = total_aggregates['cancelled_count']

            cancellation_rate = (cancelled_bookings / total_bookings * 100) if total_bookings > 0 else 0

            # Attendance Tracking (only on completed bookings for the business)
            completed_qs = bookings_qs.filter(status='completed')
            total_completed_for_attendance = completed_qs.count()
            attended_count = completed_qs.filter(attendance_marked=True, attended=True).count()
            attendance_rate = (attended_count / total_completed_for_attendance * 100) if total_completed_for_attendance > 0 else 0

            # User Retention (using all bookings in the period for this business)
            user_bookings_in_business = bookings_qs.values('user').annotate(booking_count=Count('id'))
            total_users_in_period = user_bookings_in_business.count()
            repeat_users_in_period = user_bookings_in_business.filter(booking_count__gt=1).count()
            retention_rate = (repeat_users_in_period / total_users_in_period * 100) if total_users_in_period > 0 else 0

            # --- Trends ---
            daily_trends_data = bookings_qs.annotate(
                 date=TruncDate('booking_date')
            ).values('date').annotate(
                 new_bookings=Count('id'),
                 cancelled_count=Count('id', filter=Q(status='cancelled')),
            ).order_by('date')

            processed_trends = [{
                'date': trend['date'].isoformat(),
                'new_bookings': trend['new_bookings'],
                'cancellation_rate': round((trend['cancelled_count'] / trend['new_bookings'] * 100) if trend['new_bookings'] > 0 else 0, 1)
            } for trend in daily_trends_data]

            # --- Class Insights ---
            popular_classes_data = bookings_qs.values(
                'schedule_instance__schedule__option__classId__title'
            ).annotate(
                total_bookings=Count('id'),
                unique_students=Count('user', distinct=True),
                class_completed_count=Count('id', filter=Q(status='completed')),
                class_attended_count=Count('id', filter=Q(status='completed', attendance_marked=True, attended=True)),
                class_cancelled_count=Count('id', filter=Q(status='cancelled'))
            ).order_by('-total_bookings')

            popular_classes = [{
                 'class_name': entry['schedule_instance__schedule__option__classId__title'],
                 'total_bookings': entry['total_bookings'],
                 'unique_students': entry['unique_students'],
                 'attendance_rate': round((entry['class_attended_count'] / entry['class_completed_count'] * 100) if entry['class_completed_count'] > 0 else 0, 1),
                 'cancellation_rate': round((entry['class_cancelled_count'] / entry['total_bookings'] * 100) if entry['total_bookings'] > 0 else 0, 1),
            } for entry in popular_classes_data if entry['schedule_instance__schedule__option__classId__title']]

            # --- Booking Patterns ---
            time_dist_data = bookings_qs.annotate(
                hour=ExtractHour('booking_date')
            ).values('hour').annotate(
                bookings=Count('id'),
                cancelled=Count('id', filter=Q(status='cancelled'))
            ).order_by('hour')

            time_distribution = [{'hour': entry['hour'], 'bookings': entry['bookings'], 'cancelled': entry['cancelled']} for entry in time_dist_data]

            type_dist_data = bookings_qs.values('enrollment_type').annotate(count=Count('id')).order_by('-count')
            total_bookings_for_types = bookings_qs.count()
            booking_types = [{'type': entry['enrollment_type'], 'count': entry['count'], 'percentage': round((entry['count'] / total_bookings_for_types * 100) if total_bookings_for_types > 0 else 0, 1)} for entry in type_dist_data if entry['enrollment_type']]

            # Prepare response data
            response_data = {
                 'business_id': business.businessId,
                 'business_name': business.businessName,
                 'date_range': {'start': start_date.isoformat(), 'end': end_date.isoformat()},
                 'summary': {
                     'total_bookings': total_bookings,
                     'active_bookings': total_aggregates['confirmed_count'],
                     'completed_bookings': total_aggregates['completed_count'],
                     'cancelled_bookings': cancelled_bookings,
                     'total_revenue': float(total_aggregates['total_revenue']),
                     'cancellation_rate': round(cancellation_rate, 1),
                     'attendance_rate': round(attendance_rate, 1),
                     'student_retention_rate': round(retention_rate, 1)
                 },
                 'trends': processed_trends,
                 'class_insights': { 'popular_classes': popular_classes },
                 'booking_patterns': { 'time_distribution': time_distribution, 'booking_types': booking_types }
            }

            return Response(response_data)

        except ValidationError as e:
             logger.warning(f"Validation error in booking analytics for business {business.businessId}: {e.detail}")
             return Response({'error': e.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Error in booking analytics for business {business.businessId}: {str(e)}", exc_info=True)
            return Response({'error': 'An error occurred while generating analytics.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Helper for getting date range
    def _get_date_range(self, request):
        """Get and validate date range from request parameters"""
        try:
            start_date_str = request.query_params.get('start_date')
            end_date_str = request.query_params.get('end_date')
            today = timezone.now().date()

            if start_date_str:
                start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            else:
                start_date = today - timedelta(days=30) # Default last 30 days

            if end_date_str:
                end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            else:
                end_date = today # Default up to today

            if start_date > end_date:
                raise ValidationError("Start date cannot be after end date.")

            # Convert to timezone-aware datetimes for range query consistency
            start_datetime = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
            end_datetime = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))

            return start_datetime, end_datetime

        except ValueError:
            raise ValidationError("Invalid date format. Use YYYY-MM-DD")

    # --- Block unwanted standard actions ---
    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def partial_update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def destroy(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)