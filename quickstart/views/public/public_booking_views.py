# views/student/student_booking_views.py
from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta
from django.shortcuts import get_object_or_404
from django.db.models import Prefetch
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

# Adjust import paths as needed
from ...models import Booking, ScheduleInstance, Reviews, ClassImage
from ...serializers import (
    BookingCreateSerializer,
    BookingDetailSerializer,
    StudentBookingSerializer
)

import logging
logger = logging.getLogger(__name__)

# --- Pagination ---
class StudentBookingPagination(PageNumberPagination):
    page_size = 10 # Smaller page size for student view?
    page_size_query_param = 'page_size'
    max_page_size = 50

# --- Permissions (Optional - Can rely on IsAuthenticated + object checks) ---
# Example: More explicit permission if needed
# class IsBookingOwner(BasePermission):
#     def has_object_permission(self, request, view, obj):
#         return obj.user == request.user

# --- Student ViewSet ---
class StudentBookingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Students to manage their own Bookings.
    Handles listing own bookings, retrieving details, creating, and cancelling.
    """
    serializer_class = StudentBookingSerializer # Default for list/retrieve
    permission_classes = [IsAuthenticated] # Must be logged in
    pagination_class = StudentBookingPagination
    filter_backends = [filters.OrderingFilter] # Allow ordering own bookings
    ordering_fields = ['schedule_instance__date', 'booking_date', 'status']
    ordering = ['-schedule_instance__date', '-schedule_instance__time']

    # Limit methods available to students
    http_method_names = ['get', 'post', 'head', 'options'] # Allow GET (list, retrieve), POST (create, cancel action)

    def get_serializer_class(self):
        # Override for specific actions
        if self.action == 'create':
            return BookingCreateSerializer
        elif self.action == 'retrieve':
            return BookingDetailSerializer # Use generic detail for now
        # Default is StudentBookingSerializer (for list)
        return super().get_serializer_class()

    def get_queryset(self):
        """ Filters queryset to only bookings belonging to the current authenticated user. """
        user = self.request.user
        if not user or not user.is_authenticated:
            return Booking.objects.none()

        # Base queryset optimized for student view
        return Booking.objects.filter(user=user).select_related(
            'schedule_instance__schedule__option__classId__businessId',
            'schedule_instance__schedule__option__classId',
            'schedule_instance__schedule__option',
            'review' # For checking if review exists efficiently
        ).prefetch_related(
            # Prefetch first image for the class
            Prefetch(
                'schedule_instance__schedule__option__classId__images',
                queryset=ClassImage.objects.order_by('createdAt'), # Get the first one reliably
                to_attr='prefetched_images' # Store prefetched images
            )
        ).distinct()

    def _apply_student_filters(self, queryset, request):
        # Filters for student's own booking list
        status_param = request.query_params.get('status', None) # Default: show all unless specified
        if status_param and status_param != 'all':
            queryset = queryset.filter(status=status_param)

        # Filter by upcoming/past
        when = request.query_params.get('when', None)
        today = timezone.now().date()
        if when == 'upcoming':
            queryset = queryset.filter(schedule_instance__date__gte=today)
        elif when == 'past':
            queryset = queryset.filter(schedule_instance__date__lt=today)

        return queryset

    def list(self, request, *args, **kwargs):
        """ Lists the current user's bookings. """
        queryset = self.get_queryset()
        queryset = self._apply_student_filters(queryset, request) # Apply filters
        queryset = self.filter_queryset(queryset) # Apply ordering

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True, context={'request': request})
            # Add summary counts for the user's *own* bookings
            response = self.get_paginated_response(serializer.data)
            base_count_qs = Booking.objects.filter(user=request.user)
            response.data['summary'] = {
                 'total_upcoming': base_count_qs.filter(status='confirmed', schedule_instance__date__gte=timezone.now().date()).count(),
                 'total_completed': base_count_qs.filter(status='completed').count(),
                 'total_cancelled': base_count_qs.filter(status='cancelled').count()
            }
            return response

        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        """ Retrieve details of a specific booking owned by the current user. """
        instance = self.get_object() # get_queryset already filters by user
        # Double check ownership explicitly
        if instance.user != request.user:
            raise PermissionDenied("You do not have permission to view this booking.")
        serializer = self.get_serializer(instance) # Uses BookingDetailSerializer via get_serializer_class
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        """ Creates one or more bookings based on selected slots for the current user. """
        # Permission check: User needs 'add_booking' (Django default)
        if not request.user.has_perm('quickstart.add_booking'):
             raise PermissionDenied("You do not have permission to create bookings.")

        serializer = self.get_serializer(data=request.data, context={'request': request}) # Uses BookingCreateSerializer
        serializer.is_valid(raise_exception=True)

        try:
            # Serializer create method handles transaction and logic
            booking_or_list = serializer.save() # Returns representative booking
            logger.info(f"Booking creation initiated by user {request.user.email}. Initial Instance: {serializer.context['validated_instance'].pk}")

            # TODO: Return response indicating pending payment and next steps
            # For now, return detail of the representative booking
            response_serializer = BookingDetailSerializer(booking_or_list, context={'request': request})
            return Response(response_serializer.data, status=status.HTTP_201_CREATED) # Or 202 Accepted if async payment
        except ValidationError as e:
            logger.warning(f"Booking creation failed validation for user {request.user.email}. Error: {e.detail}")
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Unexpected error creating booking for user {request.user.email}: {str(e)}", exc_info=True)
            return Response({"error": "An unexpected error occurred while creating the booking."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


    @action(detail=True, methods=['post'], url_path='cancel')
    def student_cancel(self, request, pk=None):
        """ Allows a student to cancel their own booking, respecting policy. """
        # Ensure user owns the booking via get_queryset filtering in get_object
        booking = self.get_object()
        if booking.user != request.user: # Explicit check
             raise PermissionDenied("You cannot cancel this booking.")

        # Check permission: User needs 'cancel_own_booking'
        if not request.user.has_perm('quickstart.cancel_own_booking'):
            raise PermissionDenied("You do not have permission to cancel bookings.")

        # Can only cancel confirmed or pending bookings (if payment failed etc)
        if booking.status not in ['confirmed', 'pending']:
            raise ValidationError({'status': f'Cannot cancel a booking with status "{booking.status}".'})

        cancellation_reason = request.data.get('reason', 'Cancelled by student.')

        # Check cancellation policy if the booking is 'confirmed'
        if booking.status == 'confirmed':
            try:
                schedule_instance = booking.schedule_instance
                class_option = schedule_instance.schedule.option
                policy = class_option.cancellationPolicy

                # Combine date and time, make timezone aware (assuming instance time is naive)
                instance_datetime_naive = datetime.combine(schedule_instance.date, schedule_instance.time)
                # Use Django's timezone settings for awareness
                instance_datetime_aware = timezone.make_aware(instance_datetime_naive)

                if instance_datetime_aware <= timezone.now():
                     raise ValidationError({'policy': 'Cannot cancel a class that has already started or is in the past.'})

                time_diff = instance_datetime_aware - timezone.now()
                hours_until_class = time_diff.total_seconds() / 3600

                policy_hours = {'24h': 24, '48h': 48, '72h': 72, 'flexible': float('inf')}
                required_hours = policy_hours.get(policy, 24) # Default policy?

                if hours_until_class < required_hours:
                    raise ValidationError({
                        'policy': f'Cancellation not allowed. Requires {policy} notice ({required_hours} hours).'
                    })

            except AttributeError as e:
                logger.error(f"Could not determine cancellation policy for booking {pk}. Error: {e}", exc_info=True)
                raise ValidationError({'error': 'Could not verify cancellation policy.'})
            except Exception as e: # Catch other potential errors
                logger.error(f"Error checking cancellation policy for booking {pk}: {e}", exc_info=True)
                raise ValidationError({'error': 'An error occurred checking the cancellation policy.'})

        # Process cancellation
        with transaction.atomic():
            booking.status = 'cancelled'
            booking.cancelled_at = timezone.now()
            booking.cancellation_reason = cancellation_reason
            # Handle refund logic - Mark for refund if it was paid
            if booking.payment_status == 'paid':
                booking.payment_status = 'refund_pending'
                # TODO: Trigger actual refund process (e.g., signal to payment service)
                logger.info(f"Booking {pk} cancelled by student {request.user.email}. Marked for refund.")
            else:
                 # If it wasn't paid (e.g., pending), just mark as cancelled
                 logger.info(f"Booking {pk} (Status: {booking.status}, Payment: {booking.payment_status}) cancelled by student {request.user.email}. No refund needed.")

            booking.save(update_fields=['status', 'cancelled_at', 'cancellation_reason', 'payment_status'])

        # Return updated booking details
        serializer = BookingDetailSerializer(booking, context={'request': request}) # Use detail serializer
        return Response(serializer.data)

    # --- Block unwanted actions ---
    def update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def partial_update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def destroy(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)