from unittest.mock import MagicMock
import uuid
import json
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.utils import timezone
from django.db import transaction
from decimal import Decimal
import stripe
from django.conf import settings
from quickstart.models import (
    CustomUser,
    Booking,
    Discount,
    AppliedDiscount,
    PartnerTier,  # Import the AppliedDiscount model
    Payment,
    ScheduleInstance,
)
from quickstart.serializers.public.public_booking_serializers import (
    BookingCreateSerializer,
)

from quickstart.utils.email_utils import (
    send_booking_confirmation_email,
    send_business_new_booking_email,
)

import logging

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


class CreatePaymentIntentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            logger.info(
                f"CreatePaymentIntentView - Received payment intent request: {request.data}"
            )
            applied_discount_id = request.data.get("applied_discount_id")
            selected_slots = request.data.get("selectedSlots")
            if (
                not selected_slots
                or not isinstance(selected_slots, list)
                or len(selected_slots) == 0
            ):
                return Response(
                    {"error": "selectedSlots is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            serializer_data = {
                "selectedSlots": selected_slots,
                "participants": request.data.get("participants", 1),
                "notes": request.data.get("notes", ""),
                "participant_details": request.data.get("participant_details", []),
            }

            # --- FIX: Wrap serializer validation in a transaction to allow select_for_update ---
            with transaction.atomic():
                serializer = BookingCreateSerializer(
                    data=serializer_data, context={"request": request}
                )

                if not serializer.is_valid():
                    # This will now correctly raise validation errors from within the transaction
                    return Response(
                        {"error": serializer.errors}, status=status.HTTP_400_BAD_REQUEST
                    )

                validated_instance = serializer.context.get("validated_instance")
                if not validated_instance:
                    # This check remains inside the transaction for consistency
                    return Response(
                        {"error": "Internal error: Validated instance not found."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )

            # The transaction is committed here, releasing the lock. The validated data is safe to use.

            instance = validated_instance
            option = instance.schedule.option
            booking_type = option.booking_type

            all_instances = [instance]
            if booking_type == "Full Course":
                all_instances = serializer.context.get(
                    "future_course_instances", [instance]
                )

            participants = serializer.validated_data["participants"]
            subtotal = sum(Decimal(inst.price) * participants for inst in all_instances)

            final_amount = subtotal
            discount_to_apply = None
            calculated_discount_amount = Decimal("0.00")

            if applied_discount_id:
                try:
                    discount_to_apply = Discount.objects.get(
                        id=applied_discount_id, business=option.classId.businessId
                    )
                    is_valid, reason = discount_to_apply.is_valid(
                        user=request.user, booking_total=subtotal
                    )
                    if not is_valid:
                        raise DRFValidationError(reason)

                    if discount_to_apply.discount_type == "percentage":
                        calculated_discount_amount = (
                            subtotal * (discount_to_apply.value / Decimal(100))
                        ).quantize(Decimal("0.01"))
                    elif discount_to_apply.discount_type == "fixed_amount":
                        calculated_discount_amount = discount_to_apply.value

                    calculated_discount_amount = min(
                        subtotal, calculated_discount_amount
                    )
                    final_amount = subtotal - calculated_discount_amount

                    logger.info(
                        f"Applying discount {discount_to_apply.id}. Subtotal: {subtotal}, Discount: {calculated_discount_amount}, Final: {final_amount}"
                    )

                except Discount.DoesNotExist:
                    logger.warning(
                        f"Discount ID {applied_discount_id} not found for business {option.classId.businessId.id}. Ignoring."
                    )
                except DRFValidationError as e:
                    logger.warning(
                        f"Discount {applied_discount_id} failed server-side validation: {e.detail}"
                    )
                    return Response(
                        {"error": {"discount": e.detail}},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            total_amount_for_stripe_cents = int(final_amount * 100)
            if total_amount_for_stripe_cents < 50 and final_amount > 0:
                return Response(
                    {"error": "The final amount after discount is too low to process."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            participant_details_metadata_str = json.dumps(
                serializer.validated_data.get("participant_details", [])
            )

            metadata = {
                "user_id": str(request.user.userId),
                "first_slot_id": str(instance.id),
                "participants": str(participants),
                "participant_details": participant_details_metadata_str,
                "booking_type": str(booking_type),
                "notes": str(serializer.validated_data.get("notes", "")),
                "is_course": str(booking_type == "Full Course"),
                "schedule_id": str(instance.schedule.pk),
                "start_date": str(instance.date),
                "applied_discount_id": (
                    str(discount_to_apply.id) if discount_to_apply else None
                ),
                "discount_amount": (
                    str(calculated_discount_amount) if discount_to_apply else None
                ),
            }
            if booking_type == "Full Course" and instance.schedule.end_date:
                metadata["end_date"] = str(instance.schedule.end_date)

            if total_amount_for_stripe_cents == 0:
                return Response(
                    {"error": "Free bookings are not supported in this flow."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            intent = stripe.PaymentIntent.create(
                amount=total_amount_for_stripe_cents,
                currency=getattr(settings, "STRIPE_CURRENCY", "CAD").lower(),
                payment_method_types=["card"],
                metadata={k: v for k, v in metadata.items() if v is not None},
            )

            return Response(
                {
                    "clientSecret": intent.client_secret,
                    "amount": float(final_amount.quantize(Decimal("0.01"))),
                    "total_sessions": len(all_instances),
                    "booking_type": booking_type,
                }
            )

        except ScheduleInstance.DoesNotExist:
            return Response(
                {"error": "Invalid schedule instance ID provided."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except DRFValidationError as ve:
            return Response({"error": ve.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"CreatePaymentIntentView - Error creating payment intent: {str(e)}",
                exc_info=True,
            )
            return Response(
                {"error": "An unexpected error occurred while preparing payment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ProcessBookingWebhook(APIView):
    authentication_classes = []
    permission_classes = []

    def _attempt_stripe_refund(self, payment_intent_id, reason_message=""):
        try:
            existing_payment_record = Payment.objects.filter(
                stripe_payment_intent_id=payment_intent_id
            ).first()
            if existing_payment_record and existing_payment_record.status == "refunded":
                logger.info(
                    f"Refund for PI {payment_intent_id} skipped, already marked as refunded in DB."
                )
                return True

            stripe.Refund.create(payment_intent=payment_intent_id)
            logger.info(
                f"Stripe refund successfully initiated for PaymentIntent {payment_intent_id}. Reason: {reason_message}"
            )

            if existing_payment_record:
                existing_payment_record.status = "refunded"
                existing_payment_record.refund_reason = (
                    reason_message or "Refund due to booking finalization error."
                )
                existing_payment_record.refund_date = timezone.now()
                existing_payment_record.save(
                    update_fields=["status", "refund_reason", "refund_date"]
                )
            return True
        except stripe.error.InvalidRequestError as ire:
            if "has already been refunded" in str(ire).lower():
                logger.warning(
                    f"Attempted to refund PI {payment_intent_id}, but it was already refunded on Stripe: {ire}"
                )
                if (
                    existing_payment_record
                    and existing_payment_record.status != "refunded"
                ):
                    existing_payment_record.status = "refunded"
                    existing_payment_record.refund_reason = (
                        reason_message
                        or "Already refunded on Stripe, syncing local DB."
                    )
                    existing_payment_record.refund_date = timezone.now()
                    existing_payment_record.save(
                        update_fields=["status", "refund_reason", "refund_date"]
                    )
                return True
            logger.error(
                f"CRITICAL: Stripe InvalidRequestError while trying to refund PI {payment_intent_id}: {ire}"
            )
            return False
        except stripe.StripeError as refund_err:
            logger.error(
                f"CRITICAL: Failed to refund PaymentIntent {payment_intent_id}: {refund_err}"
            )
            return False

    def post(self, request):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
        event = None

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_PAYMENTS_WEBHOOK_SECRET
            )
            logger.info(
                f"--- Booking Webhook: Received event ID={event.id}, Type={event.type} ---"
            )
        except ValueError as e:
            logger.error(f"Booking Webhook - Webhook Error: Invalid payload. {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except stripe.error.SignatureVerificationError as e:
            logger.error(f"Booking Webhook - Webhook Error: Invalid signature. {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"Booking Webhook - Webhook Error: Unexpected error constructing event. {e}",
                exc_info=True,
            )
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if event.type == "payment_intent.succeeded":
            payment_intent = event.data.object
            logger.info(
                f"Booking Webhook - PaymentIntent {payment_intent.id} succeeded."
            )
            try:
                response_data = self.handle_successful_payment(payment_intent)
                return Response(response_data or {}, status=status.HTTP_200_OK)
            except DRFValidationError as ve:
                error_detail_msg = ve.detail if hasattr(ve, "detail") else str(ve)
                logger.error(
                    f"Booking Webhook - Validation Error handling PI {payment_intent.id}: {error_detail_msg}"
                )
                self._attempt_stripe_refund(
                    payment_intent.id, f"Booking validation failed: {error_detail_msg}"
                )
                return Response(
                    {"error": error_detail_msg}, status=status.HTTP_400_BAD_REQUEST
                )
            except Exception as e:
                logger.error(
                    f"Booking Webhook - Unexpected Error processing successful PI {payment_intent.id}: {e}",
                    exc_info=True,
                )
                self._attempt_stripe_refund(
                    payment_intent.id, f"Unexpected server error: {e}"
                )
                return Response(
                    {
                        "error": "Internal server error handling payment. Your payment, if processed, will be refunded."
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        elif event.type == "payment_intent.payment_failed":
            payment_intent = event.data.object
            logger.warning(
                f'Booking Webhook - PaymentIntent {payment_intent.id} failed. Reason: {payment_intent.last_payment_error.message if payment_intent.last_payment_error else "Unknown"}'
            )
            Payment.objects.filter(
                stripe_payment_intent_id=payment_intent.id, status="pending"
            ).update(
                status="failed",
                failure_message=(
                    payment_intent.last_payment_error.message
                    if payment_intent.last_payment_error
                    else "Payment failed on Stripe."
                ),
            )
        else:
            logger.debug(f"Booking Webhook - Unhandled event type {event.type}")

        return Response(status=status.HTTP_200_OK)

    def handle_successful_payment(self, payment_intent):
        if Payment.objects.filter(
            stripe_payment_intent_id=payment_intent.id, status="succeeded"
        ).exists():
            payment_record = Payment.objects.get(
                stripe_payment_intent_id=payment_intent.id, status="succeeded"
            )
            booking = payment_record.booking
            logger.warning(
                f"Booking Webhook - PaymentIntent {payment_intent.id} has already been successfully processed. Booking ID: {booking.id}, Ref: {booking.user_facing_reference}. Skipping."
            )
            return {
                "message": "Already processed",
                "booking_id": booking.id,
                "user_facing_reference": booking.user_facing_reference,
            }

        created_bookings_for_email = []

        with transaction.atomic():
            metadata = payment_intent.metadata
            if not metadata:
                raise DRFValidationError("Payment metadata missing.")

            logger.info(
                f"Booking Webhook: Processing PI {payment_intent.id} with full metadata: {metadata}"
            )

            user_id = metadata.get("user_id")
            first_slot_id = metadata.get("first_slot_id")
            participants_str = metadata.get("participants", "1")
            participant_details_str = metadata.get("participant_details", "[]")
            try:
                participant_details = json.loads(participant_details_str)
            except json.JSONDecodeError:
                participant_details = []

            booking_type = metadata.get("booking_type")
            notes = metadata.get("notes", "")
            is_course = metadata.get("is_course") == "True"
            schedule_id_from_meta = metadata.get("schedule_id")
            start_date_str = metadata.get("start_date")
            end_date_str = metadata.get("end_date")

            applied_discount_id_str = metadata.get("applied_discount_id")
            discount_amount_str = metadata.get("discount_amount")

            required_meta_keys = [
                "user_id",
                "first_slot_id",
                "participants",
                "booking_type",
                "is_course",
                "schedule_id",
                "start_date",
            ]
            if not all(key in metadata for key in required_meta_keys):
                missing_keys = [
                    key for key in required_meta_keys if key not in metadata
                ]
                raise DRFValidationError(
                    f"Payment metadata incomplete. Missing: {', '.join(missing_keys)}"
                )

            try:
                participants = int(participants_str)
                user = CustomUser.objects.get(userId=int(user_id))
                initial_instance = ScheduleInstance.objects.select_for_update().get(
                    id=int(first_slot_id)
                )
                start_date = timezone.datetime.strptime(
                    start_date_str, "%Y-%m-%d"
                ).date()
            except (
                ValueError,
                TypeError,
                CustomUser.DoesNotExist,
                ScheduleInstance.DoesNotExist,
            ) as e:
                raise DRFValidationError(
                    f"Invalid payment metadata or related object not found: {e}"
                )

            # Get the business object to check for founding partner status
            business = initial_instance.schedule.option.classId.businessId

            discount_to_apply = None
            if applied_discount_id_str:
                try:
                    discount_to_apply = Discount.objects.get(
                        id=uuid.UUID(applied_discount_id_str)
                    )
                except (Discount.DoesNotExist, ValueError, TypeError):
                    logger.warning(
                        f"Webhook: Could not find Discount with ID '{applied_discount_id_str}' from metadata. Booking will proceed without it."
                    )

            total_calculated_discount_amount = Decimal(discount_amount_str or "0.00")

            instances_to_book = []
            if is_course:
                if not end_date_str:
                    raise DRFValidationError("End date missing for course booking.")
                end_date = timezone.datetime.strptime(end_date_str, "%Y-%m-%d").date()
                instances_to_book = list(
                    ScheduleInstance.objects.select_for_update()
                    .filter(
                        schedule_id=int(schedule_id_from_meta),
                        date__gte=start_date,
                        date__lte=end_date,
                        status="scheduled",
                    )
                    .order_by("date")
                )
            else:
                instances_to_book = [initial_instance]

            if not instances_to_book:
                raise DRFValidationError(
                    "No available sessions found. Your payment will be refunded."
                )

            for instance_check in instances_to_book:
                if not instance_check.can_accommodate(participants):
                    raise DRFValidationError(
                        f"Session on {instance_check.date.strftime('%b %d')} is now full. Your payment will be refunded."
                    )

            total_amount_from_stripe = Decimal(payment_intent.amount_received) / 100

            if business.partner_tier:
                fee_percentage = business.partner_tier.fee_percentage
            else:
                # Fallback to the default tier if for some reason a business doesn't have one
                try:
                    default_tier = PartnerTier.objects.get(is_default=True)
                    fee_percentage = default_tier.fee_percentage
                    logger.warning(
                        f"Business {business.id} was missing a partner tier. Fell back to default tier '{default_tier.name}'."
                    )
                except PartnerTier.DoesNotExist:
                    # Critical fallback if no default is configured
                    logger.error(
                        "CRITICAL: No default PartnerTier is configured in the database. Using hardcoded 13% fee."
                    )
                    fee_percentage = Decimal("13.00")

            service_fee_rate = fee_percentage / Decimal("100.0")

            calculated_service_fee = (
                total_amount_from_stripe * service_fee_rate
            ).quantize(Decimal("0.01"))

            num_instances = len(instances_to_book)
            amount_per_booking_instance = (
                (total_amount_from_stripe / num_instances)
                if num_instances > 0
                else Decimal("0.00")
            )
            discount_per_booking_instance = (
                (total_calculated_discount_amount / num_instances)
                if num_instances > 0
                else Decimal("0.00")
            )

            booking_group_id = uuid.uuid4() if len(instances_to_book) > 1 else None
            for current_sch_instance in instances_to_book:
                booking = Booking(
                    schedule_instance=current_sch_instance,
                    user=user,
                    booking_group_id=booking_group_id,
                    participants=participants,
                    participant_details=participant_details,
                    notes=notes,
                    amount_paid=amount_per_booking_instance.quantize(Decimal("0.01")),
                    status="confirmed",
                    payment_status="paid",
                    enrollment_type=booking_type,
                )
                booking.save()

                # --- FIX: Create AppliedDiscount record correctly ---
                if discount_to_apply:
                    AppliedDiscount.objects.create(
                        booking=booking,
                        discount=discount_to_apply,
                        amount_saved=discount_per_booking_instance.quantize(
                            Decimal("0.01")
                        ),
                    )
                    # Atomically increment usage count
                    discount_to_apply.redeem()

                created_bookings_for_email.append(booking)

            logger.info(
                f"Booking Webhook - Created {len(created_bookings_for_email)} booking(s) for PI {payment_intent.id}. Group ID: {booking_group_id}. Discount Applied: {discount_to_apply.id if discount_to_apply else 'None'}"
            )

            charge_details = None
            latest_charge_id = getattr(payment_intent, "latest_charge", None)
            if latest_charge_id:
                try:
                    charge_details = stripe.Charge.retrieve(latest_charge_id)
                except stripe.StripeError as e:
                    logger.warning(
                        f"Booking Webhook - Could not retrieve charge {latest_charge_id} for PI {payment_intent.id}: {e}"
                    )

            payment_record = Payment.objects.create(
                booking=created_bookings_for_email[0],
                stripe_payment_intent_id=payment_intent.id,
                stripe_charge_id=latest_charge_id,
                amount=total_amount_from_stripe,
                service_fee_amount=calculated_service_fee,
                currency=payment_intent.currency.upper(),
                status="succeeded",
                payment_method_type=(
                    payment_intent.payment_method_types[0]
                    if payment_intent.payment_method_types
                    else "card"
                ),
                metadata={"original_stripe_metadata": metadata},
            )

            if charge_details:
                # FIX: Use attribute access for Stripe objects and handle nested structure
                # This makes the code compatible with both the real Stripe object and MagicMock.
                update_fields = []

                pm_details = getattr(charge_details, "payment_method_details", None)
                if pm_details and getattr(pm_details, "type", None) == "card":
                    card_obj = getattr(pm_details, "card", None)
                    if card_obj:
                        payment_record.card_brand = getattr(card_obj, "brand", None)
                        payment_record.card_last4 = getattr(card_obj, "last4", None)
                        payment_record.card_exp_month = getattr(
                            card_obj, "exp_month", None
                        )
                        payment_record.card_exp_year = getattr(
                            card_obj, "exp_year", None
                        )
                        update_fields.extend(
                            [
                                "card_brand",
                                "card_last4",
                                "card_exp_month",
                                "card_exp_year",
                            ]
                        )

                payment_record.receipt_url = getattr(
                    charge_details, "receipt_url", None
                )
                payment_record.receipt_number = getattr(
                    charge_details, "receipt_number", None
                )
                update_fields.extend(["receipt_url", "receipt_number"])

                billing_details_obj = getattr(charge_details, "billing_details", None)
                if billing_details_obj:
                    # The billing_details object from Stripe is dict-like.
                    # The previous use of .to_dict() caused a DataError because it
                    # unexpectedly returned a string.
                    # By converting the StripeObject to a standard dict, we ensure
                    # it's in a format the Django JSONField can safely serialize.
                    payment_record.billing_details = dict(billing_details_obj)
                    update_fields.append("billing_details")

                if update_fields:
                    payment_record.save(update_fields=update_fields)

            logger.info(
                f"Booking Webhook - Created Payment record {payment_record.id} for PI {payment_intent.id}"
            )

        if created_bookings_for_email:
            first_booking = created_bookings_for_email[0]
            try:
                send_booking_confirmation_email(user, first_booking)
            except Exception as email_error:
                logger.error(
                    f"Booking Webhook - Failed to send confirmation email for booking {first_booking.id}: {email_error}",
                    exc_info=True,
                )

            try:
                # The business object is already fetched above
                business_info_obj = business
                if business_info_obj and business_info_obj.newBookingNotification:
                    recipients = {business_info_obj.owner} | set(
                        business_info_obj.managers.all()
                    )
                    for biz_user in recipients:
                        if biz_user and biz_user.email:
                            send_business_new_booking_email(biz_user, first_booking)
            except Exception as email_error_biz:
                logger.error(
                    f"Booking Webhook - Failed to send new booking notification email for booking {first_booking.id}: {email_error_biz}",
                    exc_info=True,
                )

            return {
                "booking_id": first_booking.id,
                "user_facing_reference": first_booking.user_facing_reference,
            }
        else:
            raise DRFValidationError(
                "Booking creation failed unexpectedly after payment. Your payment will be refunded."
            )
