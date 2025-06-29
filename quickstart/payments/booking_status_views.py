# quickstart/views/bookings/booking_status_views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import (
    IsAuthenticated,
)  # User must be logged in to check their own booking status
from quickstart.models import Payment, Booking  # Adjust import path

import logging

logger = logging.getLogger(__name__)


class BookingStatusByPaymentIntentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, payment_intent_id):
        if not payment_intent_id:
            return Response(
                {"error": "Payment Intent ID is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # Find the Payment record associated with this payment_intent_id
            # We assume the webhook would have created this if successful.
            payment = (
                Payment.objects.select_related("booking")
                .filter(stripe_payment_intent_id=payment_intent_id)
                .first()
            )

            if not payment:
                # Payment record not yet created by webhook, or PI is invalid
                logger.info(
                    f"Booking status check for PI {payment_intent_id}: Payment record not found yet (webhook might be pending)."
                )
                return Response(
                    {
                        "status": "pending_webhook",
                        "message": "Booking confirmation is processing.",
                    },
                    status=status.HTTP_202_ACCEPTED,
                )  # 202 Accepted indicates processing

            # Check if the user requesting is the owner of the booking associated with this payment
            # This is a crucial security check.
            if payment.booking and payment.booking.user != request.user:
                logger.warning(
                    f"User {request.user.userId} attempted to access booking status for PI {payment_intent_id} not belonging to them."
                )
                return Response(
                    {"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN
                )

            if payment.status == "succeeded" and payment.booking:
                booking = payment.booking
                logger.info(
                    f"Booking status check for PI {payment_intent_id}: Found successful payment and booking {booking.id} (Ref: {booking.user_facing_reference})."
                )
                return Response(
                    {
                        "status": "confirmed",
                        "booking_id": booking.id,
                        "user_facing_reference": booking.user_facing_reference,
                        "booking_group_id": (
                            str(booking.booking_group_id)
                            if booking.booking_group_id
                            else None
                        ),
                        "participant_details": booking.participant_details,  # Send back confirmed details
                        "message": "Booking confirmed.",
                    },
                    status=status.HTTP_200_OK,
                )
            elif payment.status == "failed":
                logger.warning(
                    f"Booking status check for PI {payment_intent_id}: Payment failed."
                )
                return Response(
                    {
                        "status": "payment_failed",
                        "message": "Payment processing failed.",
                        "failure_message": payment.failure_message,
                    },
                    status=status.HTTP_200_OK,
                )  # Still 200, but status in body indicates failure
            else:  # e.g., payment still pending, or other statuses
                logger.info(
                    f"Booking status check for PI {payment_intent_id}: Payment status is '{payment.status}'."
                )
                return Response(
                    {
                        "status": "processing",  # Or map payment.status to a more generic processing status
                        "message": f"Booking confirmation is still processing (Payment status: {payment.status}).",
                    },
                    status=status.HTTP_202_ACCEPTED,
                )

        except Exception as e:
            logger.error(
                f"Error fetching booking status for PI {payment_intent_id}: {e}",
                exc_info=True,
            )
            return Response(
                {"error": "An error occurred while fetching booking status."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
