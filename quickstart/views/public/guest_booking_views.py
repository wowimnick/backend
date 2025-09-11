# quickstart/views/bookings/guest_booking_views.py

from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta
import pytz
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import ValidationError, NotFound

from quickstart.models import Booking
from quickstart.serializers import StudentBookingDetailSerializer
from quickstart.utils.email_utils import (
    send_booking_cancellation_user_email,
    send_business_student_cancellation_email,
)
import logging

logger = logging.getLogger(__name__)


class GuestBookingCancellationView(APIView):
    """
    Handles booking cancellation for unauthenticated guest users via a secure token.
    """

    permission_classes = []
    authentication_classes = []

    def get_booking(self, token):
        """Helper to retrieve a booking by its cancellation token."""
        try:
            return Booking.objects.select_related(
                "contact", "schedule_instance__schedule__option__classId__businessId"
            ).get(cancellation_token=token)
        except (Booking.DoesNotExist, ValueError):
            raise NotFound("This cancellation link is invalid or has expired.")

    def get(self, request, token, format=None):
        """
        GET request to retrieve cancellation info, so the frontend can display a confirmation.
        This provides the frontend with the necessary details to show the user what they are cancelling.
        """
        booking = self.get_booking(token)

        if booking.status != "confirmed":
            return Response(
                {"error": "This booking is not active and cannot be cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = StudentBookingDetailSerializer(
            booking, context={"request": request}
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, token, format=None):
        """
        POST request to confirm and process the cancellation.
        """
        booking = self.get_booking(token)

        if booking.status != "confirmed":
            raise ValidationError(
                f'Cannot cancel a booking with status "{booking.status}".'
            )

        schedule_instance = booking.schedule_instance
        policy = booking.cancellation_policy
        if policy == "strict":
            raise ValidationError(
                {"policy": "This booking has a strict policy and cannot be cancelled."}
            )

        business_tz = pytz.timezone(
            schedule_instance.schedule.option.classId.businessId.business_timezone
        )
        instance_datetime_aware = business_tz.localize(
            datetime.combine(schedule_instance.date, schedule_instance.time)
        )
        now_aware = timezone.now().astimezone(business_tz)

        if instance_datetime_aware <= now_aware:
            raise ValidationError(
                {"policy": "Cannot cancel a class that has already started."}
            )

        required_hours = 0
        if policy == "custom":
            required_hours = booking.cancellation_custom_hours or 0
        else:
            policy_hours_map = {"flexible": 1, "24h": 24, "48h": 48, "72h": 72}
            required_hours = policy_hours_map.get(policy, 0)

        needs_refund_processing = False
        time_until_class = instance_datetime_aware - now_aware
        if (time_until_class.total_seconds() / 3600) < required_hours:
            raise ValidationError(
                {
                    "policy": f"Cancellation not allowed. This policy requires {required_hours} hours notice."
                }
            )

        payment = booking.payments.filter(
            status__in=["succeeded", "partially_refunded"]
        ).first()
        # A refund is only possible if the policy allows it (which we checked above) and there's money to refund
        if payment and payment.available_refund_amount > 0:
            needs_refund_processing = True

        try:
            with transaction.atomic():
                booking.status = "cancelled"
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = "Cancelled by guest via secure link."

                if needs_refund_processing:
                    booking.payment_status = "refund_pending"

                booking.cancellation_token = None

                booking.save(
                    update_fields=[
                        "status",
                        "cancelled_at",
                        "cancellation_reason",
                        "payment_status",
                        "cancellation_token",
                    ]
                )

            business_user = (
                booking.schedule_instance.schedule.option.classId.businessId.owner
            )

            send_booking_cancellation_user_email(
                user=booking.contact,
                booking=booking,
                refund_details="A refund will be processed automatically if applicable.",
            )
            send_business_student_cancellation_email(
                business_user=business_user, booking=booking
            )

            serializer = StudentBookingDetailSerializer(
                booking, context={"request": request}
            )
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(
                f"Error during guest booking cancellation for token={token}: {e}",
                exc_info=True,
            )
            return Response(
                {"detail": "An error occurred while cancelling the booking."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
