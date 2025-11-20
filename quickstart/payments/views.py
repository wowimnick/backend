import traceback
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
    CourseEnrollment,
    CustomUser,
    Booking,
    Discount,
    AppliedDiscount,
    PartnerTier,
    Payment,
    ScheduleInstance,
    Contact,
    BusinessStaff,
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
    permission_classes = []

    def post(self, request):
        request_id = str(uuid.uuid4())[:8]
        logger.info(f"[{request_id}] ===== CreatePaymentIntentView START =====")

        is_guest = not request.user or not request.user.is_authenticated
        if is_guest:
            logger.info(f"[{request_id}] Guest booking initiated.")
            guest_email = request.data.get("guest_email")
            guest_full_name = request.data.get("guest_full_name")
            guest_phone = request.data.get("guest_phone")
            if not all([guest_email, guest_full_name, guest_phone]):
                return Response(
                    {"error": "Guest email, full name, and phone number are required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            first_name, _, last_name = guest_full_name.partition(" ")
        else:
            logger.info(
                f"[{request_id}] User: {request.user.userId} ({request.user.email})"
            )

        logger.info(f"[{request_id}] Raw request data: {request.data}")

        try:
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

            # The serializer validation will lock the necessary DB rows to prevent race conditions
            with transaction.atomic():
                serializer = BookingCreateSerializer(
                    data=serializer_data,
                    context={"request": request, "is_guest": is_guest},
                )
                if not serializer.is_valid():
                    logger.error(
                        f"[{request_id}] Serializer validation failed: {serializer.errors}"
                    )
                    return Response(
                        {"error": serializer.errors}, status=status.HTTP_400_BAD_REQUEST
                    )

                # Retrieve validated instances from context to use them
                instance = serializer.context.get("validated_instance")
                option = instance.schedule.option
                booking_type = option.booking_type
                business = instance.schedule.option.classId.businessId

            logger.info(
                f"[{request_id}] Validation passed for Instance ID: {instance.id}, Type: {booking_type}"
            )

            guest_contact = None
            if is_guest:
                guest_contact, created = Contact.objects.update_or_create(
                    business=business,
                    email__iexact=guest_email,
                    defaults={
                        "first_name": first_name,
                        "last_name": last_name,
                        "phone_number": guest_phone,
                        "source": "guest_booking",
                        "email": guest_email,
                    },
                )
                logger.info(
                    f"[{request_id}] {'Created' if created else 'Updated'} Contact for guest: {guest_email}"
                )

            participants = serializer.validated_data["participants"]
            notes = serializer.validated_data.get("notes", "")
            participant_details = serializer.validated_data.get(
                "participant_details", []
            )

            all_instances = (
                serializer.context.get("future_course_instances", [instance])
                if booking_type == "Full Course"
                else [instance]
            )

            # --- Pricing and Discount Calculation ---
            if booking_type == "Full Course":
                # For a course, the price is fixed on the Schedule, not per-instance
                subtotal = Decimal(instance.schedule.price) * participants
            else:
                # For single sessions, sum the price of each instance
                subtotal = (
                    sum([Decimal(inst.price) for inst in all_instances]) * participants
                )

            final_amount = subtotal
            discount_to_apply = None
            calculated_discount_amount = Decimal("0.00")

            if applied_discount_id:
                try:
                    discount_to_apply = Discount.objects.get(
                        id=applied_discount_id, business=business
                    )
                    is_valid_user = None if is_guest else request.user
                    is_valid, reason = discount_to_apply.is_valid(
                        user=is_valid_user, booking_total=subtotal
                    )
                    if not is_valid:
                        raise DRFValidationError(reason)

                    if discount_to_apply.discount_type == "percentage":
                        calculated_discount_amount = (
                            subtotal * (discount_to_apply.value / Decimal(100))
                        ).quantize(Decimal("0.01"))
                    else:  # fixed_amount
                        calculated_discount_amount = discount_to_apply.value

                    final_amount = subtotal - min(subtotal, calculated_discount_amount)
                except (Discount.DoesNotExist, DRFValidationError) as e:
                    error_detail = (
                        e.detail
                        if isinstance(e, DRFValidationError)
                        else "Invalid discount code."
                    )
                    return Response(
                        {"error": {"discount": error_detail}},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            subtotal_after_discount = final_amount
            tax_amount = (subtotal_after_discount * HST_RATE).quantize(Decimal("0.01"))
            grand_total = subtotal_after_discount + tax_amount
            total_amount_for_stripe_cents = int(grand_total * 100)

            # --- Check for Stripe Minimum (only if price > 0) ---
            if 0 < total_amount_for_stripe_cents < 50:
                return Response(
                    {"error": "The final amount is too low to process."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # ==========================================
            #  FREE BOOKING FLOW (Price is 0)
            # ==========================================
            if total_amount_for_stripe_cents == 0:
                logger.info(f"[{request_id}] Processing FREE booking (Total: $0.00).")

                first_booking = None
                booking_group_id = None
                
                try:
                    with transaction.atomic():
                        if booking_type == "Full Course":
                            booking_group_id = uuid.uuid4()
                            enrollment = CourseEnrollment.objects.create(
                                schedule=instance.schedule,
                                user=request.user if not is_guest else None,
                                contact=guest_contact if is_guest else None,
                                booking_group_id=booking_group_id,
                                total_sessions=len(all_instances),
                                participants=participants,
                                status="active",  # Directly active
                                total_amount_paid=Decimal("0.00"),
                                cancellation_policy=option.cancellationPolicy,
                                cancellation_custom_hours=option.cancellationCustomHours,
                                cancellation_refund_percentage=option.cancellationRefundPercentage,
                            )
                            logger.info(f"[{request_id}] Created ACTIVE Free CourseEnrollment: {enrollment.id}")

                            bookings_to_create = []
                            for session_num, inst in enumerate(all_instances, start=1):
                                bookings_to_create.append(
                                    Booking(
                                        user=request.user if not is_guest else None,
                                        contact=guest_contact if is_guest else None,
                                        schedule_instance=inst,
                                        enrollment_type="Full Course",
                                        booking_group_id=booking_group_id,
                                        course_session_number=session_num,
                                        participants=participants,
                                        participant_details=participant_details,
                                        notes=notes,
                                        amount_paid=Decimal("0.00"),
                                        status="confirmed", # Directly confirmed
                                        payment_status="paid",
                                        cancellation_policy=enrollment.cancellation_policy,
                                        cancellation_custom_hours=enrollment.cancellation_custom_hours,
                                        cancellation_refund_percentage=enrollment.cancellation_refund_percentage,
                                    )
                                )
                            created_bookings = Booking.objects.bulk_create(bookings_to_create)
                            first_booking = created_bookings[0]
                            
                            # Generate ref for first booking
                            first_booking.user_facing_reference = first_booking._generate_user_facing_reference()
                            first_booking.save(update_fields=['user_facing_reference'])
                            logger.info(f"[{request_id}] Bulk-created {len(created_bookings)} CONFIRMED free Bookings.")

                        else: # Single Session
                            first_booking = Booking.objects.create(
                                schedule_instance=instance,
                                user=request.user if not is_guest else None,
                                contact=guest_contact if is_guest else None,
                                participants=participants,
                                participant_details=participant_details,
                                notes=notes,
                                amount_paid=Decimal("0.00"),
                                status="confirmed", # Directly confirmed
                                payment_status="paid",
                                enrollment_type="Single Session",
                                cancellation_policy=option.cancellationPolicy,
                                cancellation_refund_percentage=option.cancellationRefundPercentage,
                                cancellation_custom_hours=option.cancellationCustomHours,
                            )
                            first_booking.user_facing_reference = first_booking._generate_user_facing_reference()
                            first_booking.save(update_fields=['user_facing_reference'])
                            logger.info(f"[{request_id}] Created CONFIRMED single free Booking: {first_booking.id}")

                        if is_guest:
                            first_booking.cancellation_token = uuid.uuid4()
                            first_booking.save(update_fields=['cancellation_token'])

                        # Create 'Succeeded' Payment Record for 0 amount (for bookkeeping)
                        Payment.objects.create(
                            booking=first_booking,
                            stripe_payment_intent_id=f"free_booking_{uuid.uuid4()}", 
                            amount=Decimal("0.00"),
                            tax_amount=Decimal("0.00"),
                            currency="CAD",
                            status="succeeded",
                            metadata={
                                "is_free": True, 
                                "notes": notes, 
                                "applied_discount_id": str(discount_to_apply.id) if discount_to_apply else None
                            }
                        )
                        logger.info(f"[{request_id}] Created $0.00 Payment record.")

                    # --- Emails ---
                    recipient_user = request.user if not is_guest else None
                    recipient_contact = guest_contact if is_guest else None

                    if recipient_user:
                        logger.info(f"[{request_id}] Sending free booking confirmation email to User.")
                        send_booking_confirmation_email(recipient_user, first_booking)
                    elif recipient_contact:
                         logger.info(f"[{request_id}] Sending free booking confirmation email to Guest.")
                         send_booking_confirmation_email(recipient_contact, first_booking)
                    
                    if business.newBookingNotification:
                        logger.info(f"[{request_id}] Sending new booking notification to business.")
                        recipients = {business.owner}
                        staff_to_notify = BusinessStaff.objects.filter(
                            business=business,
                            status="accepted",
                            role__permissions__codename="receive_booking_notifications",
                        ).select_related("user")
                        
                        for staff in staff_to_notify:
                            if staff.user: recipients.add(staff.user)
                        
                        for r in recipients:
                            if r and r.email:
                                send_business_new_booking_email(r, first_booking)

                    response_data = {
                        "booking_id": first_booking.id,
                        "user_facing_reference": first_booking.user_facing_reference,
                        "booking_group_id": str(booking_group_id) if booking_group_id else None,
                        "participant_details": participant_details,
                        "status": "confirmed",
                        "message": "Free booking confirmed successfully."
                    }
                    logger.info(f"[{request_id}] ===== CreatePaymentIntentView SUCCESS (FREE) =====")
                    return Response(response_data)
                
                except Exception as e:
                    logger.error(f"[{request_id}] Error processing free booking: {str(e)}", exc_info=True)
                    return Response(
                        {"error": "An error occurred while processing the free booking."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )

            # ==========================================
            #  PAID BOOKING FLOW (Standard Stripe)
            # ==========================================
            # --- Create Pending Database Records ---
            first_booking = None
            booking_group_id = None
            pending_payment = None

            with transaction.atomic():
                if booking_type == "Full Course":
                    booking_group_id = uuid.uuid4()
                    enrollment = CourseEnrollment.objects.create(
                        schedule=instance.schedule,
                        user=request.user if not is_guest else None,
                        contact=guest_contact if is_guest else None,
                        booking_group_id=booking_group_id,
                        total_sessions=len(all_instances),
                        participants=participants,
                        status="pending",
                        total_amount_paid=Decimal("0.00"),
                        cancellation_policy=option.cancellationPolicy,
                        cancellation_custom_hours=option.cancellationCustomHours,
                        cancellation_refund_percentage=option.cancellationRefundPercentage,
                    )
                    logger.info(
                        f"[{request_id}] Created pending CourseEnrollment: {enrollment.id}"
                    )

                    bookings_to_create = [
                        Booking(
                            user=request.user if not is_guest else None,
                            contact=guest_contact if is_guest else None,
                            schedule_instance=inst,
                            enrollment_type="Full Course",
                            booking_group_id=booking_group_id,
                            course_session_number=session_num,
                            participants=participants,
                            participant_details=participant_details,
                            notes=notes,
                            amount_paid=Decimal("0.00"),
                            status="pending",
                            payment_status="pending",
                            cancellation_policy=enrollment.cancellation_policy,
                            cancellation_custom_hours=enrollment.cancellation_custom_hours,
                            cancellation_refund_percentage=enrollment.cancellation_refund_percentage,
                        )
                        for session_num, inst in enumerate(all_instances, start=1)
                    ]
                    created_bookings = Booking.objects.bulk_create(bookings_to_create)
                    first_booking = created_bookings[0]
                    logger.info(
                        f"[{request_id}] Bulk-created {len(created_bookings)} pending Bookings."
                    )
                else:  # Single Session
                    first_booking = Booking.objects.create(
                        schedule_instance=instance,
                        user=request.user if not is_guest else None,
                        contact=guest_contact if is_guest else None,
                        participants=participants,
                        participant_details=participant_details,
                        notes=notes,
                        amount_paid=grand_total,
                        status="pending",
                        payment_status="pending",
                        enrollment_type="Single Session",
                        cancellation_policy=option.cancellationPolicy,
                        cancellation_refund_percentage=option.cancellationRefundPercentage,
                        cancellation_custom_hours=option.cancellationCustomHours,
                    )
                    logger.info(
                        f"[{request_id}] Created single pending Booking: {first_booking.id}"
                    )

                currency_code = getattr(settings, "STRIPE_CURRENCY", "cad")

                pending_payment = Payment.objects.create(
                    booking=first_booking,
                    stripe_payment_intent_id="temp", 
                    amount=grand_total,
                    tax_amount=tax_amount,
                    currency=currency_code.upper(),
                    status="pending",
                )
                logger.info(
                    f"[{request_id}] Created pending Payment record: {pending_payment.id}"
                )

            # --- Create Stripe Intent and Finalize ---
            metadata = {
                "participants": str(participants),
                "booking_type": booking_type,
                "notes": notes,
                "applied_discount_id": (
                    str(discount_to_apply.id) if discount_to_apply else None
                ),
                "discount_amount": str(calculated_discount_amount),
                "subtotal_after_discount": str(subtotal_after_discount),
                "tax_amount": str(tax_amount),
                "is_guest": str(is_guest),
                "payment_db_id": str(pending_payment.id),
                "first_booking_db_id": str(first_booking.id),
                "booking_group_id": str(booking_group_id) if booking_group_id else None,
            }
            if is_guest:
                metadata["guest_contact_id"] = str(guest_contact.id)
            else:
                metadata["user_id"] = str(request.user.userId)

            try:
                intent = stripe.PaymentIntent.create(
                    amount=total_amount_for_stripe_cents,
                    currency=currency_code.lower(), 
                    payment_method_types=["card"],
                    metadata={k: v for k, v in metadata.items() if v is not None},
                )

                pending_payment.stripe_payment_intent_id = intent.id
                pending_payment.save(update_fields=["stripe_payment_intent_id"])
                logger.info(
                    f"[{request_id}] Created Stripe PaymentIntent: {intent.id} and updated Payment record."
                )

                response_data = {
                    "clientSecret": intent.client_secret,
                    "amount": float(grand_total),
                    "total_sessions": len(all_instances),
                    "booking_type": booking_type,
                }
                logger.info(
                    f"[{request_id}] ===== CreatePaymentIntentView SUCCESS ====="
                )
                return Response(response_data)
            except Exception as e:
                logger.error(
                    f"[{request_id}] Stripe or DB update error: {e}", exc_info=True
                )
                # Rollback: Delete the pending records we just created
                with transaction.atomic():
                    if booking_group_id:
                        CourseEnrollment.objects.filter(
                            booking_group_id=booking_group_id
                        ).delete()
                        Booking.objects.filter(
                            booking_group_id=booking_group_id
                        ).delete()
                    elif first_booking:
                        first_booking.delete()
                    if pending_payment:
                        pending_payment.delete()
                logger.info(
                    f"[{request_id}] Rolled back pending DB records due to Stripe error."
                )
                return Response(
                    {
                        "error": "An error occurred while contacting the payment provider."
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        except DRFValidationError as ve:
            error_detail = ve.detail if hasattr(ve, "detail") else str(ve)
            logger.error(f"[{request_id}] Validation error: {error_detail}")
            return Response({"error": error_detail}, status=status.HTTP_400_BAD_REQUEST)
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
                    f"[{webhook_id}] !!! --- UNEXPECTED WEBHOOK EXCEPTION --- !!!"
                )
                logger.error(f"[{webhook_id}] Exception Type: {type(e).__name__}")
                logger.error(f"[{webhook_id}] Exception Message: {e}")
                logger.error(
                    f"[{webhook_id}] Full Traceback:\n{traceback.format_exc()}"
                )
                logger.error(f"[{webhook_id}] !!! --- END OF TRACEBACK --- !!!")

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

    def handle_course_payment_success(self, payment_intent, webhook_id):
        """
        Handle successful payment for a course enrollment.
        Updates CourseEnrollment and all session Bookings.

        This is called from handle_successful_payment when enrollment_type is "Full Course".
        """
        try:
            with transaction.atomic():
                # Get booking group ID from metadata
                booking_group_id = payment_intent.metadata.get("booking_group_id")
                if not booking_group_id:
                    logger.error(
                        f"[{webhook_id}] No booking_group_id in payment intent metadata"
                    )
                    raise DRFValidationError(
                        "Missing booking_group_id in payment metadata"
                    )

                # Get enrollment
                try:
                    enrollment = CourseEnrollment.objects.select_for_update().get(
                        booking_group_id=booking_group_id
                    )
                except CourseEnrollment.DoesNotExist:
                    logger.error(
                        f"[{webhook_id}] CourseEnrollment not found for booking_group_id {booking_group_id}"
                    )
                    raise DRFValidationError(
                        f"Course enrollment not found for booking group {booking_group_id}"
                    )

                # Update enrollment status
                enrollment.status = "active"
                enrollment.total_amount_paid = (
                    Decimal(str(payment_intent.amount_received)) / 100
                )
                enrollment.save(update_fields=["status", "total_amount_paid"])

                logger.info(
                    f"[{webhook_id}] Updated CourseEnrollment {enrollment.id} to active, amount: ${enrollment.total_amount_paid}"
                )

                # Get all bookings for this course
                bookings = list(
                    Booking.objects.select_for_update()
                    .filter(booking_group_id=booking_group_id)
                    .order_by("course_session_number")
                )

                if not bookings:
                    logger.error(
                        f"[{webhook_id}] No bookings found for booking_group_id {booking_group_id}"
                    )
                    raise DRFValidationError(f"No bookings found for course enrollment")

                # Calculate price per session
                session_price = enrollment.total_amount_paid / len(bookings)
                session_price = session_price.quantize(Decimal("0.01"))

                # Update all bookings
                for booking in bookings:
                    booking.status = "confirmed"
                    booking.payment_status = "paid"
                    booking.amount_paid = session_price

                    # Generate reference only for first session
                    if (
                        booking.course_session_number == 1
                        and not booking.user_facing_reference
                    ):
                        booking.user_facing_reference = (
                            booking._generate_user_facing_reference()
                        )

                # Bulk update for efficiency
                Booking.objects.bulk_update(
                    bookings,
                    [
                        "status",
                        "payment_status",
                        "amount_paid",
                        "user_facing_reference",
                    ],
                )

                logger.info(
                    f"[{webhook_id}] Successfully processed payment for course enrollment {enrollment.id}, "
                    f"updated {len(bookings)} session bookings"
                )

                # TODO: Send confirmation email (when email system is ready)
                # from quickstart.utils.email_utils import send_course_enrollment_confirmation_email
                # send_course_enrollment_confirmation_email(enrollment)

                # Create payment record
                from quickstart.models import Payment

                payment_record = Payment.objects.create(
                    booking=bookings[0],  # Link to first booking
                    stripe_payment_intent_id=payment_intent.id,
                    amount=enrollment.total_amount_paid,
                    status="succeeded",
                )

                logger.info(
                    f"[{webhook_id}] Created payment record {payment_record.id} for course enrollment"
                )

                return {"message": "Course payment processed successfully"}

        except Exception as e:
            logger.error(
                f"[{webhook_id}] Error processing course payment success: {e}",
                exc_info=True,
            )
            raise

    def handle_successful_payment(self, payment_intent, webhook_id):
        if Payment.objects.filter(
            stripe_payment_intent_id=payment_intent.id, status="succeeded"
        ).exists():
            logger.warning(
                f"[{webhook_id}] DUPLICATE: PI {payment_intent.id} already processed."
            )
            return {"message": "Already processed"}

        enrollment_type = payment_intent.metadata.get("enrollment_type")

        if enrollment_type == "Full Course":
            logger.info(
                f"[{webhook_id}] Detected course payment, routing to course handler"
            )
            return self.handle_course_payment_success(payment_intent, webhook_id)

        with transaction.atomic():
            try:
                payment_record = Payment.objects.select_for_update().get(
                    stripe_payment_intent_id=payment_intent.id, status="pending"
                )
                pending_booking = Booking.objects.select_for_update().get(
                    pk=payment_record.booking.pk, status="pending"
                )
            except (Payment.DoesNotExist, Booking.DoesNotExist):
                raise DRFValidationError(
                    "Could not find a corresponding pending booking or payment for this successful payment. Refunding to prevent lost funds."
                )

            metadata = payment_intent.metadata
            participants = pending_booking.participants
            initial_instance = pending_booking.schedule_instance

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

            if pending_booking.contact and not pending_booking.user:
                pending_booking.cancellation_token = uuid.uuid4()
                logger.info(
                    f"[{webhook_id}] Generated cancellation token for guest booking {pending_booking.id}"
                )

            pending_booking.save()
            logger.info(f"[{webhook_id}] Booking {pending_booking.id} confirmed.")

            # --- CALCULATE FEES AND UPDATE PAYMENT RECORD ---
            grand_total = Decimal(payment_intent.amount_received) / 100
            total_tax = Decimal(metadata.get("tax_amount", "0.00"))
            subtotal_after_discount = Decimal(
                metadata.get("subtotal_after_discount", "0.00")
            )
            business = initial_instance.schedule.option.classId.businessId

            # MODIFICATION: Check for the widget flag to determine the fee percentage.
            if metadata.get("booking_source") == "widget":
                fee_percentage = Decimal("6.00")
                logger.info(
                    f"[{webhook_id}] Applying fixed 6% widget fee for booking {pending_booking.id}."
                )
            else:
                fee_percentage = (
                    business.partner_tier.fee_percentage
                    if business.partner_tier
                    else PartnerTier.objects.get(is_default=True).fee_percentage
                )
                logger.info(
                    f"[{webhook_id}] Applying partner tier fee ({fee_percentage}%) for booking {pending_booking.id}."
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

        recipient_user = pending_booking.user
        recipient_contact = pending_booking.contact

        logger.info(
            f"[{webhook_id}] Preparing to send confirmation emails for Booking ID: {pending_booking.id}"
        )
        logger.info(f"[{webhook_id}]   - Recipient User object: {recipient_user}")
        logger.info(f"[{webhook_id}]   - Recipient Contact object: {recipient_contact}")

        if recipient_user:
            logger.info(
                f"[{webhook_id}]   - Identified as REGISTERED USER. Attempting to send email to {recipient_user.email}."
            )
            send_booking_confirmation_email(recipient_user, pending_booking)
        elif recipient_contact:
            logger.info(
                f"[{webhook_id}]   - Identified as GUEST. Attempting to send email to {recipient_contact.email}."
            )
            send_booking_confirmation_email(recipient_contact, pending_booking)
        else:
            logger.error(
                f"[{webhook_id}] CRITICAL: No recipient (user or contact) found for Booking ID {pending_booking.id}. Cannot send confirmation email."
            )

        if business.newBookingNotification:
            logger.info(
                f"[{webhook_id}] Preparing to send new booking notification to business."
            )

            # Start with the business owner as a recipient
            recipients = {business.owner}

            # Find all active staff members whose role has the new permission
            staff_to_notify = BusinessStaff.objects.filter(
                business=business,
                status="accepted",
                role__permissions__codename="receive_booking_notifications",
            ).select_related("user")

            for staff in staff_to_notify:
                if staff.user:
                    recipients.add(staff.user)

            logger.info(
                f"Notification recipients: {[r.email for r in recipients if r]}"
            )

            for recipient in recipients:
                if recipient and recipient.email:
                    send_business_new_booking_email(recipient, pending_booking)

        return {
            "booking_id": pending_booking.id,
            "user_facing_reference": pending_booking.user_facing_reference,
        }