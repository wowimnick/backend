# quickstart/payments/views.py

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
from django.db.models.functions import Coalesce
from django.db.models import Sum
from quickstart.models import (
    CustomUser,
    Booking,
    Discount,
    AppliedDiscount,
    PartnerTier,
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
HST_RATE = Decimal("0.13")


class CreatePaymentIntentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request_id = str(uuid.uuid4())[:8]  # Short ID for tracking this request
        logger.info(f"[{request_id}] ===== CreatePaymentIntentView START =====")
        logger.info(
            f"[{request_id}] User: {request.user.userId} ({request.user.email})"
        )
        logger.info(f"[{request_id}] Raw request data: {request.data}")

        try:
            # Parse and validate basic request data
            applied_discount_id = request.data.get("applied_discount_id")
            selected_slots = request.data.get("selectedSlots")
            logger.info(f"[{request_id}] Applied discount ID: {applied_discount_id}")
            logger.info(f"[{request_id}] Selected slots: {selected_slots}")

            if (
                not selected_slots
                or not isinstance(selected_slots, list)
                or len(selected_slots) == 0
            ):
                logger.error(f"[{request_id}] Invalid selectedSlots provided")
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
            logger.info(f"[{request_id}] Serializer data prepared: {serializer_data}")

            # Validate booking data
            logger.info(f"[{request_id}] Starting database transaction for validation")
            with transaction.atomic():
                logger.info(f"[{request_id}] Creating BookingCreateSerializer")
                serializer = BookingCreateSerializer(
                    data=serializer_data, context={"request": request}
                )

                logger.info(f"[{request_id}] Validating serializer...")
                if not serializer.is_valid():
                    logger.error(
                        f"[{request_id}] Serializer validation failed: {serializer.errors}"
                    )
                    return Response(
                        {"error": serializer.errors}, status=status.HTTP_400_BAD_REQUEST
                    )

                logger.info(f"[{request_id}] Serializer validation passed")
                validated_instance = serializer.context.get("validated_instance")
                if not validated_instance:
                    logger.error(
                        f"[{request_id}] Validated instance not found in serializer context"
                    )
                    return Response(
                        {"error": "Internal error: Validated instance not found."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )

                logger.info(
                    f"[{request_id}] Validated instance found: {validated_instance.id}"
                )

            # Get booking details
            instance = validated_instance
            option = instance.schedule.option
            booking_type = option.booking_type
            logger.info(
                f"[{request_id}] Booking instance: {instance.id}, Option: {option.optionId}, Type: {booking_type}"
            )

            all_instances = [instance]
            if booking_type == "Full Course":
                logger.info(
                    f"[{request_id}] Full Course booking - getting future instances"
                )
                all_instances = serializer.context.get(
                    "future_course_instances", [instance]
                )
                logger.info(
                    f"[{request_id}] Future course instances found: {[inst.id for inst in all_instances]}"
                )

            participants = serializer.validated_data["participants"]
            logger.info(f"[{request_id}] Number of participants: {participants}")

            # Calculate pricing
            logger.info(
                f"[{request_id}] Calculating pricing for {len(all_instances)} instances"
            )
            instance_prices = []
            for inst in all_instances:
                inst_price = Decimal(inst.price) * participants
                instance_prices.append(inst_price)
                logger.info(
                    f"[{request_id}] Instance {inst.id} ({inst.date}): {inst.price} x {participants} = {inst_price}"
                )

            subtotal = sum(instance_prices)
            logger.info(f"[{request_id}] Subtotal calculated: {subtotal}")

            final_amount = subtotal
            discount_to_apply = None
            calculated_discount_amount = Decimal("0.00")

            # Handle discount if provided
            if applied_discount_id:
                logger.info(
                    f"[{request_id}] Processing discount ID: {applied_discount_id}"
                )
                try:
                    discount_to_apply = Discount.objects.get(
                        id=applied_discount_id, business=option.classId.businessId
                    )
                    logger.info(
                        f"[{request_id}] Discount found: {discount_to_apply.code} ({discount_to_apply.discount_type})"
                    )

                    logger.info(f"[{request_id}] Validating discount eligibility")
                    is_valid, reason = discount_to_apply.is_valid(
                        user=request.user, booking_total=subtotal
                    )
                    if not is_valid:
                        logger.warning(
                            f"[{request_id}] Discount validation failed: {reason}"
                        )
                        raise DRFValidationError(reason)

                    logger.info(f"[{request_id}] Discount validation passed")

                    if discount_to_apply.discount_type == "percentage":
                        logger.info(
                            f"[{request_id}] Calculating percentage discount: {discount_to_apply.value}%"
                        )
                        calculated_discount_amount = (
                            subtotal * (discount_to_apply.value / Decimal(100))
                        ).quantize(Decimal("0.01"))
                    elif discount_to_apply.discount_type == "fixed_amount":
                        logger.info(
                            f"[{request_id}] Applying fixed discount: ${discount_to_apply.value}"
                        )
                        calculated_discount_amount = discount_to_apply.value

                    calculated_discount_amount = min(
                        subtotal, calculated_discount_amount
                    )
                    final_amount = subtotal - calculated_discount_amount

                    logger.info(f"[{request_id}] Discount calculation complete:")
                    logger.info(f"[{request_id}]   - Subtotal: {subtotal}")
                    logger.info(
                        f"[{request_id}]   - Discount amount: {calculated_discount_amount}"
                    )
                    logger.info(f"[{request_id}]   - Final amount: {final_amount}")

                except Discount.DoesNotExist:
                    logger.warning(
                        f"[{request_id}] Discount ID {applied_discount_id} not found for business {option.classId.businessId.businessId}"
                    )
                except DRFValidationError as e:
                    logger.warning(
                        f"[{request_id}] Discount validation error: {e.detail}"
                    )
                    return Response(
                        {"error": {"discount": e.detail}},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            else:
                logger.info(f"[{request_id}] No discount applied")

            # Calculate taxes
            subtotal_after_discount = final_amount
            tax_amount = (subtotal_after_discount * HST_RATE).quantize(Decimal("0.01"))
            grand_total = subtotal_after_discount + tax_amount

            logger.info(f"[{request_id}] Tax calculation:")
            logger.info(
                f"[{request_id}]   - Subtotal after discount: {subtotal_after_discount}"
            )
            logger.info(f"[{request_id}]   - HST rate: {HST_RATE} ({HST_RATE*100}%)")
            logger.info(f"[{request_id}]   - Tax amount: {tax_amount}")
            logger.info(f"[{request_id}]   - Grand total: {grand_total}")

            # Convert to cents for Stripe
            total_amount_for_stripe_cents = int(grand_total * 100)
            logger.info(
                f"[{request_id}] Stripe amount (cents): {total_amount_for_stripe_cents}"
            )

            if total_amount_for_stripe_cents < 50 and grand_total > 0:
                logger.error(
                    f"[{request_id}] Amount too low for Stripe processing: {grand_total}"
                )
                return Response(
                    {"error": "The final amount after discount is too low to process."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Prepare metadata
            participant_details_metadata_str = json.dumps(
                serializer.validated_data.get("participant_details", [])
            )

            logger.info(f"[{request_id}] Preparing Stripe metadata")
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
                "subtotal_after_discount": str(subtotal_after_discount),
                "tax_amount": str(tax_amount),
                "hst_rate": str(HST_RATE),
            }
            if booking_type == "Full Course" and instance.schedule.end_date:
                metadata["end_date"] = str(instance.schedule.end_date)

            logger.info(f"[{request_id}] Stripe metadata prepared: {metadata}")

            # Check for free booking
            if total_amount_for_stripe_cents == 0:
                logger.error(f"[{request_id}] Free booking attempted (not supported)")
                return Response(
                    {"error": "Free bookings are not supported in this flow."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Create Stripe PaymentIntent
            logger.info(f"[{request_id}] Creating Stripe PaymentIntent")
            stripe_currency = getattr(settings, "STRIPE_CURRENCY", "CAD").lower()
            logger.info(f"[{request_id}] Stripe currency: {stripe_currency}")

            intent = stripe.PaymentIntent.create(
                amount=total_amount_for_stripe_cents,
                currency=stripe_currency,
                payment_method_types=["card"],
                metadata={k: v for k, v in metadata.items() if v is not None},
            )

            logger.info(f"[{request_id}] PaymentIntent created successfully:")
            logger.info(f"[{request_id}]   - ID: {intent.id}")
            logger.info(f"[{request_id}]   - Amount: {intent.amount}")
            logger.info(f"[{request_id}]   - Currency: {intent.currency}")
            logger.info(f"[{request_id}]   - Status: {intent.status}")

            # Create pending booking and payment record immediately
            logger.info(f"[{request_id}] Creating pending booking and payment record")

            # Snapshot the cancellation policy NOW
            logger.info(
                f"[{request_id}] Snapshotting cancellation policy from option {option.optionId}"
            )
            snapshotted_policy = option.cancellationPolicy
            snapshotted_refund_percent = option.cancellationRefundPercentage
            snapshotted_custom_hours = option.cancellationCustomHours

            logger.info(f"[{request_id}] Cancellation policy snapshot:")
            logger.info(f"[{request_id}]   - Policy: {snapshotted_policy}")
            logger.info(
                f"[{request_id}]   - Refund percentage: {snapshotted_refund_percent}"
            )
            logger.info(f"[{request_id}]   - Custom hours: {snapshotted_custom_hours}")

            # Create pending booking
            pending_booking = Booking.objects.create(
                schedule_instance=instance,
                user=request.user,
                participants=participants,
                participant_details=serializer.validated_data.get(
                    "participant_details", []
                ),
                notes=serializer.validated_data.get("notes", ""),
                amount_paid=grand_total.quantize(Decimal("0.01")),
                status="pending",
                payment_status="pending",
                enrollment_type=booking_type,
                cancellation_policy=snapshotted_policy,
                cancellation_refund_percentage=snapshotted_refund_percent,
                cancellation_custom_hours=snapshotted_custom_hours,
            )

            logger.info(f"[{request_id}] Pending booking created:")
            logger.info(f"[{request_id}]   - ID: {pending_booking.id}")
            logger.info(f"[{request_id}]   - Status: {pending_booking.status}")
            logger.info(
                f"[{request_id}]   - Payment status: {pending_booking.payment_status}"
            )
            logger.info(
                f"[{request_id}]   - Cancellation policy: {pending_booking.cancellation_policy}"
            )

            # Create pending payment record
            pending_payment = Payment.objects.create(
                booking=pending_booking,
                stripe_payment_intent_id=intent.id,
                amount=grand_total,
                tax_amount=tax_amount,
                currency=intent.currency.upper(),
                status="pending",
                payment_method_type="card",
                metadata={"original_stripe_metadata": metadata},
            )

            logger.info(f"[{request_id}] Pending payment record created:")
            logger.info(f"[{request_id}]   - ID: {pending_payment.id}")
            logger.info(f"[{request_id}]   - Status: {pending_payment.status}")
            logger.info(f"[{request_id}]   - Amount: {pending_payment.amount}")

            response_data = {
                "clientSecret": intent.client_secret,
                "amount": float(grand_total.quantize(Decimal("0.01"))),
                "total_sessions": len(all_instances),
                "booking_type": booking_type,
            }

            logger.info(f"[{request_id}] Response data prepared: {response_data}")
            logger.info(f"[{request_id}] ===== CreatePaymentIntentView SUCCESS =====")

            return Response(response_data)

        except ScheduleInstance.DoesNotExist:
            logger.error(f"[{request_id}] ScheduleInstance not found")
            return Response(
                {"error": "Invalid schedule instance ID provided."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except DRFValidationError as ve:
            logger.error(f"[{request_id}] DRF Validation error: {ve.detail}")
            return Response({"error": ve.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"[{request_id}] Unexpected error in CreatePaymentIntentView: {str(e)}",
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
        logger.info(
            f"REFUND: Attempting refund for PaymentIntent {payment_intent_id}. Reason: {reason_message}"
        )
        try:
            stripe.Refund.create(payment_intent=payment_intent_id)
            return True
        except stripe.StripeError as e:
            logger.error(f"REFUND: CRITICAL - Stripe error during refund: {e}")
            return False

    def _mark_booking_as_failed(self, payment_intent_id, reason):
        """
        Helper function to find pending records and mark them as failed.
        This is used when a paid booking cannot be fulfilled.
        """
        try:
            with transaction.atomic():
                # Use select_for_update to lock the rows during the update
                payment_record = Payment.objects.select_for_update().get(
                    stripe_payment_intent_id=payment_intent_id, status="pending"
                )
                booking_record = Booking.objects.select_for_update().get(
                    pk=payment_record.booking.pk, status="pending"
                )

                # Update Payment record
                payment_record.status = "failed"
                payment_record.failure_message = reason
                payment_record.save()

                # Update Booking record
                booking_record.status = "cancelled"  # The booking itself is cancelled
                booking_record.payment_status = "failed"
                booking_record.cancellation_reason = reason
                booking_record.cancelled_at = timezone.now()
                booking_record.save()

                logger.warning(
                    f"Marked Booking {booking_record.id} and Payment {payment_record.id} as FAILED. Reason: {reason}"
                )
        except (Payment.DoesNotExist, Booking.DoesNotExist):
            logger.error(
                f"FAILURE_MARKER: Could not find pending booking/payment for PI {payment_intent_id} to mark as failed."
            )
        except Exception as e:
            logger.error(
                f"FAILURE_MARKER: An unexpected error occurred while marking PI {payment_intent_id} as failed: {str(e)}"
            )

    def post(self, request):
        webhook_id = str(uuid.uuid4())[:8]
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_PAYMENTS_WEBHOOK_SECRET
            )
        except Exception as e:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if event.type == "payment_intent.succeeded":
            payment_intent = event.data.object
            logger.info(
                f"[{webhook_id}] Processing payment_intent.succeeded for PI: {payment_intent.id}"
            )
            try:
                response_data = self.handle_successful_payment(
                    payment_intent, webhook_id
                )
                return Response(response_data or {}, status=status.HTTP_200_OK)
            except DRFValidationError as ve:
                error_msg = str(ve.detail)
                logger.error(
                    f"[{webhook_id}] Validation error in webhook: {error_msg}",
                    exc_info=True,
                )
                # ADDED: Mark records as failed before refunding
                self._mark_booking_as_failed(
                    payment_intent.id, f"Booking validation failed: {error_msg}"
                )
                self._attempt_stripe_refund(
                    payment_intent.id, f"Booking validation failed: {error_msg}"
                )
                return Response(
                    {"error": error_msg}, status=status.HTTP_400_BAD_REQUEST
                )
            except Exception as e:
                logger.error(
                    f"[{webhook_id}] Unexpected error in webhook: {e}", exc_info=True
                )
                # ADDED: Mark records as failed before refunding
                self._mark_booking_as_failed(
                    payment_intent.id, f"Unexpected server error: {str(e)}"
                )
                self._attempt_stripe_refund(
                    payment_intent.id, f"Unexpected server error: {e}"
                )
                return Response(
                    {"error": "Internal server error"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        elif event.type == "payment_intent.payment_failed":
            payment_intent = event.data.object
            failure_message = (
                payment_intent.last_payment_error.message
                if payment_intent.last_payment_error
                else "Payment failed."
            )

            # CHANGED: Update both Payment and the associated Booking
            try:
                with transaction.atomic():
                    payment_to_fail = Payment.objects.select_for_update().get(
                        stripe_payment_intent_id=payment_intent.id, status="pending"
                    )
                    booking_to_fail = payment_to_fail.booking

                    payment_to_fail.status = "failed"
                    payment_to_fail.failure_message = failure_message
                    payment_to_fail.save()

                    if booking_to_fail and booking_to_fail.status == "pending":
                        booking_to_fail.status = "cancelled"
                        booking_to_fail.payment_status = "failed"
                        booking_to_fail.cancellation_reason = "Payment was declined."
                        booking_to_fail.cancelled_at = timezone.now()
                        booking_to_fail.save()
                        logger.info(
                            f"Set booking {booking_to_fail.id} to cancelled due to failed payment."
                        )

            except Payment.DoesNotExist:
                logger.warning(
                    f"Received payment_failed webhook for PI {payment_intent.id}, but no corresponding pending payment was found."
                )
            except Exception as e:
                logger.error(
                    f"Error processing payment_failed webhook for PI {payment_intent.id}: {str(e)}"
                )

        return Response(status=status.HTTP_200_OK)

    # ... (handle_successful_payment method remains the same) ...
    def handle_successful_payment(self, payment_intent, webhook_id):
        if Payment.objects.filter(
            stripe_payment_intent_id=payment_intent.id, status="succeeded"
        ).exists():
            logger.warning(
                f"[{webhook_id}] DUPLICATE: PI {payment_intent.id} already processed."
            )
            return {"message": "Already processed"}

        with transaction.atomic():
            try:
                payment_record = Payment.objects.select_for_update().get(
                    stripe_payment_intent_id=payment_intent.id, status="pending"
                )
                pending_booking = Booking.objects.select_for_update().get(
                    pk=payment_record.booking.pk, status="pending"
                )
            except (Payment.DoesNotExist, Booking.DoesNotExist):
                # CHANGED: Raise a more specific error message
                raise DRFValidationError(
                    "Could not find a corresponding pending booking or payment for this successful payment. Refunding to prevent lost funds."
                )

            metadata = payment_intent.metadata
            participants = pending_booking.participants
            initial_instance = pending_booking.schedule_instance

            # Re-validate capacity, excluding the current pending booking
            other_participants = (
                initial_instance.bookings.filter(status__in=["confirmed", "pending"])
                .exclude(pk=pending_booking.pk)
                .aggregate(total=Coalesce(Sum("participants"), 0))["total"]
            )
            if (initial_instance.max_participants - other_participants) < participants:
                raise DRFValidationError(
                    f"Session on {initial_instance.date.strftime('%b %d')} is now full."
                )

            # --- UPDATE PENDING BOOKING TO CONFIRMED ---
            pending_booking.status = "confirmed"
            pending_booking.payment_status = "paid"
            pending_booking.save()  # This generates the user_facing_reference
            logger.info(f"[{webhook_id}] Booking {pending_booking.id} confirmed.")

            # --- CALCULATE FEES AND UPDATE PAYMENT RECORD ---
            grand_total = Decimal(payment_intent.amount_received) / 100
            total_tax = Decimal(metadata.get("tax_amount", "0.00"))
            subtotal_after_discount = Decimal(
                metadata.get("subtotal_after_discount", "0.00")
            )
            business = initial_instance.schedule.option.classId.businessId

            fee_percentage = (
                business.partner_tier.fee_percentage
                if business.partner_tier
                else PartnerTier.objects.get(is_default=True).fee_percentage
            )

            service_fee_rate = fee_percentage / Decimal("100.0")
            platform_fee_amount = (subtotal_after_discount * service_fee_rate).quantize(
                Decimal("0.01")
            )
            platform_fee_tax = (platform_fee_amount * HST_RATE).quantize(
                Decimal("0.01")
            )
            business_payout_tax = total_tax - platform_fee_tax
            business_net_revenue = subtotal_after_discount - platform_fee_amount
            net_payout_to_business = business_net_revenue + business_payout_tax

            charge_details = (
                stripe.Charge.retrieve(payment_intent.latest_charge)
                if payment_intent.latest_charge
                else None
            )

            payment_record.stripe_charge_id = payment_intent.latest_charge
            payment_record.status = "succeeded"
            payment_record.amount = grand_total
            payment_record.tax_amount = total_tax
            payment_record.platform_fee_amount = platform_fee_amount
            payment_record.platform_fee_tax = platform_fee_tax
            payment_record.net_payout_amount = net_payout_to_business
            payment_record.metadata = {"original_stripe_metadata": dict(metadata)}

            if charge_details and charge_details.payment_method_details.card:
                payment_record.card_brand = (
                    charge_details.payment_method_details.card.brand
                )
                payment_record.card_last4 = (
                    charge_details.payment_method_details.card.last4
                )
            if charge_details:
                payment_record.receipt_url = charge_details.receipt_url

            payment_record.save()
            logger.info(
                f"[{webhook_id}] Payment {payment_record.id} updated to succeeded."
            )

        # Send emails after the transaction is successfully committed
        send_booking_confirmation_email(pending_booking.user, pending_booking)
        if business.newBookingNotification:
            for recipient in {business.owner} | set(business.managers.all()):
                if recipient and recipient.email:
                    send_business_new_booking_email(recipient, pending_booking)

        return {
            "booking_id": pending_booking.id,
            "user_facing_reference": pending_booking.user_facing_reference,
        }
