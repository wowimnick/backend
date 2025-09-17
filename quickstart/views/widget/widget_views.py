# quickstart/views/widget/widget_views.py
import uuid
import stripe
import logging
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, generics
from rest_framework.exceptions import ValidationError, NotFound, PermissionDenied

from quickstart.models import (
    BusinessInfo,
    ClassesMain,
    ScheduleInstance,
    Booking,
    Contact,
    Payment,
    ClassOption,
)
from quickstart.serializers.widget.widget_serializers import (
    # We will refine these serializers as needed
    WidgetBusinessConfigSerializer,
    WidgetClassSerializer,
    WidgetScheduleInstanceSerializer,
    GuestBookingCreateSerializer,
)

# A custom permission that just checks if the middleware found a business
from rest_framework.permissions import BasePermission

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


# This permission class is now much simpler, relying on the middleware
class IsValidWidgetRequest(BasePermission):
    message = "Invalid or missing Business ID."

    def has_permission(self, request, view):
        # The middleware has already done the lookup and attached the business.
        # If it's not there, the request is invalid.
        if not request.business_context:
            return False
        # Also check if the business is active and verified
        return (
            request.business_context.isActive
            and request.business_context.verificationStatus == "verified"
        )


# --- VIEWS ---


class WidgetConfigView(generics.RetrieveAPIView):
    permission_classes = [IsValidWidgetRequest]
    serializer_class = WidgetBusinessConfigSerializer

    def get_object(self):
        return self.request.business_context

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data
        data["stripe_publishable_key"] = settings.STRIPE_PUBLIC_KEY
        return Response(data)


class WidgetClassListView(generics.ListAPIView):
    permission_classes = [IsValidWidgetRequest]
    serializer_class = WidgetClassSerializer

    def get_queryset(self):
        return ClassesMain.objects.filter(
            businessId=self.request.business_context, status="active"
        )


class WidgetAvailabilityView(APIView):
    permission_classes = [IsValidWidgetRequest]

    def get(self, request, *args, **kwargs):
        # This view remains largely the same, its job is just to report availability
        option_id = request.query_params.get("option_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        if not all([option_id, start_date, end_date]):
            raise ValidationError(
                "`option_id`, `start_date`, and `end_date` are required."
            )
        instances = ScheduleInstance.get_available_in_range(
            option_id, start_date, end_date
        )
        serializer = WidgetScheduleInstanceSerializer(instances, many=True)
        return Response(serializer.data)


class CreateGuestPaymentIntentView(APIView):
    """
    Backend-authoritative price calculation and Payment Intent creation.
    """

    permission_classes = [IsValidWidgetRequest]

    def post(self, request, *args, **kwargs):
        instance_id = request.data.get("schedule_instance_id")
        participants = int(request.data.get("participants", 1))

        if not instance_id or participants < 1:
            raise ValidationError(
                "A valid schedule_instance_id and at least 1 participant are required."
            )

        business = request.business_context
        try:
            instance = ScheduleInstance.objects.select_related(
                "schedule__option__classId__partner_tier"
            ).get(id=instance_id, schedule__option__classId__businessId=business)
        except ScheduleInstance.DoesNotExist:
            raise NotFound("The selected session is not available.")

        if not instance.can_accommodate(participants):
            raise ValidationError(
                f"Not enough spots available. Only {instance.available_spots} left."
            )

        # --- PRODUCTION-READY FEE CALCULATION ---
        # All calculations use Decimal for financial accuracy.

        subtotal = instance.price * Decimal(participants)

        # 1. Platform Fee (6% of subtotal)
        # Use the partner_tier if it exists, otherwise default to a higher rate.
        fee_percentage = (
            business.partner_tier.fee_percentage
            if business.partner_tier
            else Decimal("6.00")
        )
        platform_fee = (subtotal * (fee_percentage / Decimal("100"))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # 2. Tax (13% HST on the subtotal)
        hst_rate = Decimal("0.13")
        tax_on_subtotal = (subtotal * hst_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # 3. Grand Total charged to the customer
        total_amount_charged = subtotal + tax_on_subtotal

        # 4. Final net amount that will be paid out to the business
        net_payout_amount = subtotal - platform_fee

        # Convert to cents for Stripe API
        final_amount_cents = int(total_amount_charged * 100)

        try:
            payment_intent = stripe.PaymentIntent.create(
                amount=final_amount_cents,
                currency=business.currency.lower(),
                automatic_payment_methods={"enabled": True},
                transfer_group=f"booking_widget_{uuid.uuid4()}",  # For grouping transfers
                # CRITICAL: Store our calculated breakdown in metadata for data integrity.
                metadata={
                    "business_id": business.businessId,
                    "schedule_instance_id": instance.id,
                    "participants": participants,
                    "booking_source": "widget",
                    "subtotal_cents": int(subtotal * 100),
                    "tax_cents": int(tax_on_subtotal * 100),
                    "platform_fee_cents": int(platform_fee * 100),
                    "net_payout_cents": int(net_payout_amount * 100),
                },
            )
            return Response({"client_secret": payment_intent.client_secret})
        except stripe.StripeError as e:
            logger.error(
                f"Stripe API error during PI creation for business {business.businessId}: {e}",
                exc_info=True,
            )
            return Response(
                {"error": "A payment processing error occurred. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class GuestBookingCreateView(generics.CreateAPIView):
    """
    Verifies payment and creates all necessary records in a single atomic transaction.
    """

    permission_classes = [IsValidWidgetRequest]
    serializer_class = GuestBookingCreateSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        business = request.business_context

        try:
            # 1. Retrieve and verify the Payment Intent from Stripe
            pi = stripe.PaymentIntent.retrieve(data["payment_intent_id"])
            if pi.status != "succeeded":
                raise ValidationError(
                    f"Payment was not successful. Status: {pi.status}"
                )

            metadata = pi.metadata
            instance_id = int(metadata["schedule_instance_id"])
            participants = int(metadata["participants"])

            # --- ATOMIC TRANSACTION & RACE CONDITION LOCK ---
            with transaction.atomic():
                instance = ScheduleInstance.objects.select_for_update().get(
                    id=instance_id
                )

                if not instance.can_accommodate(participants):
                    # Race condition detected! Someone booked while this user was paying.
                    logger.error(
                        f"RACE CONDITION: Overbooking attempt on instance {instance.id}. PI: {pi.id}. Triggering refund."
                    )
                    # We have their money but can't provide the service. We MUST refund.
                    stripe.Refund.create(payment_intent=pi.id)
                    raise ValidationError(
                        "Sorry, the last spots were booked just as you were paying. Your card has not been charged (or has been automatically refunded)."
                    )

                # 2. Get or Create Guest Contact Record
                contact, _ = Contact.objects.get_or_create(
                    business=business,
                    email__iexact=data["email"],
                    defaults={
                        "first_name": data["first_name"],
                        "last_name": data["last_name"],
                        "phone_number": data.get("phone_number", ""),
                        "source": "widget_booking",
                    },
                )

                # 3. Create the Booking Record
                class_option = instance.schedule.option
                booking = Booking.objects.create(
                    contact=contact,
                    schedule_instance=instance,
                    participants=participants,
                    amount_paid=Decimal(pi.amount_received / 100.0),
                    status="confirmed",
                    payment_status="paid",
                    payout_status="pending",
                    enrollment_type=class_option.booking_type,
                    cancellation_policy=class_option.cancellationPolicy,
                    cancellation_custom_hours=class_option.cancellationCustomHours,
                    cancellation_refund_percentage=class_option.cancellationRefundPercentage,
                    cancellation_token=uuid.uuid4(),
                )

                # 4. Create the Authoritative Payment Record from Metadata
                Payment.objects.create(
                    booking=booking,
                    stripe_payment_intent_id=pi.id,
                    stripe_charge_id=pi.latest_charge,
                    status="succeeded",
                    amount=Decimal(metadata["subtotal_cents"]) / 100
                    + Decimal(metadata["tax_cents"]) / 100,
                    tax_amount=Decimal(metadata["tax_cents"]) / 100,
                    platform_fee_amount=Decimal(metadata["platform_fee_cents"]) / 100,
                    net_payout_amount=Decimal(metadata["net_payout_cents"]) / 100,
                    currency=business.currency,
                    payment_method_type=(
                        pi.payment_method_types[0]
                        if pi.payment_method_types
                        else "card"
                    ),
                    receipt_url=(
                        pi.charges.data[0].receipt_url if pi.charges.data else None
                    ),
                    created_at=timezone.now(),
                )

            # --- Post-Transaction Actions (e.g., Email Notifications) ---
            # send_guest_booking_confirmation_email(booking)
            # send_new_booking_notification_to_business(booking)

            return Response(
                {
                    "message": "Booking confirmed!",
                    "booking_reference": booking.user_facing_reference,
                },
                status=status.HTTP_201_CREATED,
            )

        except (stripe.error.StripeError, ValidationError) as e:
            logger.warning(
                f"Validation or Stripe error in guest booking creation: {e}",
                exc_info=True,
            )
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"CRITICAL: Unhandled exception in guest booking creation for PI {data.get('payment_intent_id')}: {e}",
                exc_info=True,
            )
            return Response(
                {
                    "error": "An unexpected server error occurred. Our team has been notified."
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
