# views/student/student_booking_views.py
from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta
from django.shortcuts import get_object_or_404
from django.db.models import Prefetch
import pytz
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

from ...utils.email_utils import (
        send_booking_cancellation_user_email,
        send_business_student_cancellation_email,
    )

from ...utils.email_utils import send_booking_cancellation_user_email

import logging
logger = logging.getLogger(__name__)

# --- Pagination ---
class StudentBookingPagination(PageNumberPagination):
    page_size = 10 # Smaller page size for student view?
    page_size_query_param = 'page_size'
    max_page_size = 50

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
        if not request.user.has_perm('quickstart.add_booking'): # Or your custom permission
             raise PermissionDenied("You do not have permission to create bookings.")

        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        try:
            booking_or_list = serializer.save() # Serializer's create method now handles participant_details
            logger.info(f"Booking creation initiated by user {request.user.email}. Initial Instance: {serializer.context['validated_instance'].pk}")

            response_serializer = BookingDetailSerializer(booking_or_list, context={'request': request})
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e: # DRF ValidationError
            logger.warning(f"Booking creation failed validation for user {request.user.email}. Error: {e.detail}")
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except DjangoValidationError as e: # Django Core ValidationError
            logger.warning(f"Booking creation failed Django validation for user {request.user.email}. Error: {e.message_dict}")
            return Response(e.message_dict, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Unexpected error creating booking for user {request.user.email}: {str(e)}", exc_info=True)
            return Response({"error": "An unexpected error occurred while creating the booking."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


    @action(detail=True, methods=['post'], url_path='cancel')
    def student_cancel(self, request, pk=None):
        """ Allows a student to cancel their own booking, respecting policy. """
        booking = self.get_object() # get_queryset filters by user
        if booking.user != request.user: # Explicit check just in case
             raise PermissionDenied("You cannot cancel this booking.")

        if not request.user.has_perm('quickstart.cancel_own_booking'):
            raise PermissionDenied("You do not have permission to cancel bookings.")

        if booking.status not in ['confirmed', 'pending']:
            raise ValidationError({'status': f'Cannot cancel a booking with status "{booking.status}".'})

        cancellation_reason = request.data.get('reason', 'Cancelled by student.')

        # --- Determine Refund Status & Policy Check ---
        refund_details_message = "As per the cancellation policy, no refund was applicable for this cancellation."
        needs_refund_processing = False
        can_cancel_based_on_policy = True # Assume true initially
        payment = None # Define payment variable

        if booking.status == 'confirmed':
            try:
                schedule_instance = booking.schedule_instance
                class_option = schedule_instance.schedule.option
                policy = class_option.cancellationPolicy

                business_timezone_str = class_option.classId.businessId.business_timezone
                try:
                    business_tz = pytz.timezone(business_timezone_str)
                except pytz.UnknownTimeZoneError:
                    logger.error(f"Unknown timezone '{business_timezone_str}' for business {class_option.classId.businessId.pk}. Defaulting to UTC for cancellation check.")
                    business_tz = timezone.utc # Fallback, or raise a more specific error

                instance_datetime_naive = datetime.combine(schedule_instance.date, schedule_instance.time)
                instance_datetime_aware = business_tz.localize(instance_datetime_naive) # Localize to business's timezone

                if instance_datetime_aware <= timezone.now(): # Comparison is now correct
                     can_cancel_based_on_policy = False
                     raise ValidationError({'policy': 'Cannot cancel a class that has already started or is in the past.'})

                policy_hours_map = {'24h': 24, '48h': 48, '72h': 72, 'flexible': float('inf')}
                required_hours = policy_hours_map.get(policy, 0)

                if required_hours != float('inf'):
                    time_diff = instance_datetime_aware - timezone.now()
                    hours_until_class = time_diff.total_seconds() / 3600
                    if hours_until_class < required_hours:
                        can_cancel_based_on_policy = False
                        raise ValidationError({'policy': f'Cancellation not allowed. Requires {policy} notice ({required_hours} hours).'})

                # Fetch payment here for later use
                payment = booking.payments.filter(status__in=['succeeded', 'partially_refunded']).order_by('-created_at').first()

                if can_cancel_based_on_policy and payment: # Check if policy allows and payment exists
                     needs_refund_processing = True # Assume refund needed if paid and policy allows

            except AttributeError as e:
                logger.error(f"Could not determine cancellation policy/payment for booking {pk}. Error: {e}", exc_info=True)
                raise ValidationError({'error': 'Could not verify cancellation policy/payment. Cancellation aborted.'})
            except ValidationError as ve:
                 raise ve
            except Exception as e:
                logger.error(f"Unexpected error checking cancellation policy for booking {pk}: {e}", exc_info=True)
                raise ValidationError({'error': 'An error occurred checking the cancellation policy. Cancellation aborted.'})
        # --- End Policy Check ---

        # Process cancellation if allowed
        try:
            # --- Get Business User BEFORE atomic transaction ---
            # Needs careful error handling if relations are missing
            business_user_to_notify = None
            try:
                business_owner = booking.schedule_instance.schedule.option.classId.businessId.owner
                if business_owner and business_owner.email:
                     business_user_to_notify = business_owner # Notify owner first
                # Optionally add logic to notify managers as well
                # business_managers = booking.schedule_instance.schedule.option.classId.businessId.managers.all()
            except AttributeError:
                 logger.error(f"Could not find business owner to notify for student cancellation of booking {pk}")

            # --- Start Atomic Transaction ---
            with transaction.atomic():
                booking.status = 'cancelled'
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = cancellation_reason

                if needs_refund_processing and payment and payment.available_refund_amount > 0:
                    refund_amount_display = payment.available_refund_amount
                    refund_details_message = f"A refund of ${refund_amount_display:.2f} will be processed."
                    booking.payment_status = 'refund_pending' # Indicate refund is initiated
                    # TODO: Trigger refund task here!
                    logger.info(f"Booking {pk} cancelled by student {request.user.email}. Marked payment as refund_pending.")
                else:
                    logger.info(f"Booking {pk} cancelled by student {request.user.email}. No refund required or possible.")
                    # Keep original payment_status (e.g., 'paid' if paid but no refund, or 'pending' if cancelled before payment)

                booking.save(update_fields=['status', 'cancelled_at', 'cancellation_reason', 'payment_status'])

            # --- Email Notifications (AFTER transaction) ---
            # 1. Notify the Student
            try:
                send_booking_cancellation_user_email(
                    user=request.user,
                    booking=booking,
                    refund_details=refund_details_message
                )
                logger.info(f"User cancellation email prepared/queued for booking {pk}")
            except Exception as email_error:
                logger.error(f"Failed to send user cancellation email for booking {pk}: {email_error}", exc_info=True)

            # 2. *** ADDED: Notify the Business User ***
            if business_user_to_notify:
                 try:
                     send_business_student_cancellation_email(
                         business_user=business_user_to_notify,
                         booking=booking
                     )
                     logger.info(f"Business student cancellation email prepared/queued for booking {pk} to {business_user_to_notify.email}")
                 except Exception as email_error:
                     logger.error(f"Failed to send business student cancellation email for booking {pk}: {email_error}", exc_info=True)
            else:
                 logger.warning(f"Skipped business notification for cancellation of booking {pk} as owner email was not found.")
            # *** END ADDED ***


            # Return updated booking details
            serializer = BookingDetailSerializer(booking, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)

        except ValidationError as ve:
             # Catch validation errors raised during policy check
             return Response(ve.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Error during booking cancellation save/email for pk={pk}: {e}", exc_info=True)
            return Response(
                {'detail': 'An error occurred while cancelling the booking.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    # --- Block unwanted actions --- (Keep as is)
    def update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def partial_update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def destroy(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
