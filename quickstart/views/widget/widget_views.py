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
from rest_framework.exceptions import ValidationError, NotFound
from django.db.models import Q, Count, Sum, Subquery, OuterRef, IntegerField, Prefetch
from django.db.models.functions import Coalesce

from quickstart.models import (
    BusinessInfo,
    ClassesMain,
    ScheduleInstance,
    Booking,
    Contact,
    Payment,
    ClassImage,
    ClassOption,
    WidgetSubscription,
)
from quickstart.serializers.widget.widget_serializers import (
    WidgetBusinessConfigSerializer,
    WidgetClassSerializer,
    WidgetScheduleInstanceSerializer,
    GuestBookingCreateSerializer,
)

from quickstart.utils.permissions import IsValidWidgetRequest

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


def _business_has_active_widget_subscription(business):
    """True if widget subscription is not required, or business has an active subscription."""
    if not getattr(settings, "WIDGET_SUBSCRIPTION_REQUIRED", False):
        return True
    now = timezone.now()
    return WidgetSubscription.objects.filter(
        business=business,
        status__in=["active", "trialing"],
        current_period_end__gt=now,
    ).exists()


class WidgetConfigView(generics.RetrieveAPIView):
    permission_classes = [IsValidWidgetRequest]
    serializer_class = WidgetBusinessConfigSerializer

    def get_object(self):
        return self.request.business_context

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if not _business_has_active_widget_subscription(instance):
            return Response(
                {
                    "error": "widget_subscription_required",
                    "message": "An active widget subscription is required. Please subscribe in your dashboard.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
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
        ).prefetch_related(
            "options__schedules",
            Prefetch("images", queryset=ClassImage.objects.order_by("-isCover")),
        )


class WidgetAvailabilityView(APIView):
    permission_classes = [IsValidWidgetRequest]

    def get(self, request, *args, **kwargs):
        logger.info(f"Widget availability request: {request.query_params}")

        option_id = request.query_params.get("option_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        if not all([option_id, start_date, end_date]):
            raise ValidationError(
                "`option_id`, `start_date`, and `end_date` are required."
            )

        try:
            # Verify the option belongs to this business
            option = ClassOption.objects.get(
                optionId=option_id,
                classId__businessId=request.business_context,
                classId__status="active",
            )
        except ClassOption.DoesNotExist:
            logger.error(
                f"ClassOption {option_id} not found for business {request.business_context.businessId}"
            )
            raise ValidationError("Invalid option_id for this business.")

        # Correctly defined subquery to calculate the sum of confirmed participants.
        # This groups bookings by the schedule instance, annotates the sum, and selects that value.
        confirmed_participants_subquery = (
            Booking.objects.filter(schedule_instance=OuterRef("pk"), status="confirmed")
            .values("schedule_instance")
            .annotate(total=Sum("participants"))
            .values("total")
        )

        # Get schedule instances with current booking counts
        instances = (
            ScheduleInstance.objects.filter(
                schedule__option=option,
                date__range=[start_date, end_date],
                status="scheduled",
            )
            .annotate(
                # Use Coalesce to handle instances with no bookings (returns 0 instead of None)
                confirmed_participants=Coalesce(
                    Subquery(
                        confirmed_participants_subquery, output_field=IntegerField()
                    ),
                    0,
                )
            )
            .select_related("schedule")
            .order_by("date", "time")
        )

        logger.info(
            f"Found {instances.count()} schedule instances for option {option_id}"
        )

        # Group by date
        availability_by_date = {}
        for instance in instances:
            confirmed_participants = instance.confirmed_participants or 0
            available_spots = max(0, instance.max_participants - confirmed_participants)

            # Only include instances with available spots
            if available_spots > 0:
                date_str = instance.date.isoformat()
                if date_str not in availability_by_date:
                    availability_by_date[date_str] = []

                availability_by_date[date_str].append(
                    {
                        "instance_id": instance.id,
                        "time": instance.time.strftime("%H:%M:%S"),
                        "duration": instance.duration,
                        "price": str(instance.price),
                        "max_participants": instance.max_participants,
                        "available_spots": available_spots,
                        "min_participants": instance.min_participants,
                    }
                )

        logger.info(f"Returning availability for {len(availability_by_date)} dates")
        return Response(availability_by_date)


class CreateGuestPaymentIntentView(APIView):
    """
    Backend-authoritative price calculation and Payment Intent creation.
    """

    permission_classes = [IsValidWidgetRequest]

    def post(self, request, *args, **kwargs):
        business = request.business_context
        if not _business_has_active_widget_subscription(business):
            return Response(
                {
                    "error": "widget_subscription_required",
                    "message": "An active widget subscription is required to accept bookings.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        instance_id = request.data.get("schedule_instance_id")
        participants = int(request.data.get("participants", 1))

        if not instance_id or participants < 1:
            raise ValidationError(
                "A valid schedule_instance_id and at least 1 participant are required."
            )

        business = request.business_context
        try:
            instance = ScheduleInstance.objects.select_related(
                "schedule__option__classId__businessId__partner_tier"
            ).get(id=instance_id, schedule__option__classId__businessId=business)
        except ScheduleInstance.DoesNotExist:
            raise NotFound("The selected session is not available.")

        if not instance.can_accommodate(participants):
            raise ValidationError(
                f"Not enough spots available. Only {instance.available_spots} left."
            )

        # Fee calculation: 4% platform fee on top of class price (customer pays class price + 4%).
        subtotal = instance.price * Decimal(participants)
        fee_percentage = Decimal("4.00")
        platform_fee = (subtotal * (fee_percentage / Decimal("100"))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # Tax calculation (on subtotal)
        hst_rate = Decimal("0.13")
        tax_on_subtotal = (subtotal * hst_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # Customer pays: subtotal + platform_fee (4%) + tax. Business payout is subtotal - platform_fee (Stripe fee deducted at payout).
        total_amount_charged = subtotal + platform_fee + tax_on_subtotal
        net_payout_amount = subtotal - platform_fee
        final_amount_cents = int(total_amount_charged * 100)

        try:
            payment_intent = stripe.PaymentIntent.create(
                amount=final_amount_cents,
                currency=business.currency.lower(),
                automatic_payment_methods={"enabled": True},
                transfer_group=f"booking_widget_{uuid.uuid4()}",
                metadata={
                    "business_id": business.businessId,
                    "schedule_instance_id": instance.id,
                    "participants": participants,
                    "booking_source": "widget",  # MODIFICATION: Explicitly flag as a widget booking
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
            pi = stripe.PaymentIntent.retrieve(data["payment_intent_id"])
            if pi.status != "succeeded":
                raise ValidationError(
                    f"Payment was not successful. Status: {pi.status}"
                )

            metadata = pi.metadata
            instance_id = int(metadata["schedule_instance_id"])
            participants = int(metadata["participants"])

            with transaction.atomic():
                instance = ScheduleInstance.objects.select_for_update().get(
                    id=instance_id
                )

                if not instance.can_accommodate(participants):
                    logger.error(
                        f"RACE CONDITION: Overbooking attempt on instance {instance.id}. PI: {pi.id}"
                    )
                    stripe.Refund.create(payment_intent=pi.id)
                    raise ValidationError(
                        "Sorry, the last spots were booked just as you were paying. Your card has not been charged."
                    )

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
