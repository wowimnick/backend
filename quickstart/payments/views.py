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
# Added Schedule to imports
from quickstart.models import (
    CourseEnrollment,
    CustomUser,
    Booking,
    Discount,
    AppliedDiscount,
    PartnerTier,
    Payment,
    ScheduleInstance,
    Schedule, 
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
                
                # Determine booking_type based on Option configuration
                # The serializer might assume Single Session if not explicitly told, 
                # but we trust the Option configuration for the final decision.
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

            # --- CRITICAL FIX: FETCH ALL SIBLING SCHEDULES FOR COURSES ---
            all_instances = []
            
            if booking_type == "Full Course":
                # If Full Course, we must find ALL schedules that match this group
                # (Same Option, Start Date, End Date, Time, Price)
                representative_schedule = instance.schedule
                
                sibling_schedules = Schedule.objects.filter(
                    option=representative_schedule.option,
                    start_date=representative_schedule.start_date,
                    end_date=representative_schedule.end_date,
                    time=representative_schedule.time,
                    price=representative_schedule.price
                )
                
                # Fetch ALL future instances for ALL these schedules
                all_instances = list(ScheduleInstance.objects.filter(
                    schedule__in=sibling_schedules,
                    status='scheduled',
                    date__gte=timezone.now().date()
                ).order_by('date'))
                
                if not all_instances:
                    return Response(
                         {"error": "No upcoming sessions found for this course."},
                         status=status.HTTP_400_BAD_REQUEST
                    )

                # RE-VALIDATE CAPACITY for siblings
                # The serializer only checked the instance the user clicked.
                # We must ensure the other days (e.g., Wednesday) also have space.
                for group_inst in all_instances:
                    if not group_inst.can_accommodate(participants):
                         return Response(
                            {"error": f"Session on {group_inst.date} does not have enough capacity."},
                            status=status.HTTP_400_BAD_REQUEST
                        )
            else:
                # Single Session - just use the validated instance
                all_instances = [instance]

            # --- Pricing and Discount Calculation ---
            if booking_type == "Full Course":
                # For a course, the price is fixed on the Schedule per student, not per session.
                # We use the representative instance's schedule price.
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
                        created_bookings = []
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
                            # Track generated refs to prevent collisions within the batch
                            generated_refs = set()

                            for session_num, inst in enumerate(all_instances, start=1):
                                b = Booking(
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
                                # Generate reference for EACH booking
                                while True:
                                    ref = b._generate_user_facing_reference()
                                    if ref not in generated_refs:
                                        b.user_facing_reference = ref
                                        generated_refs.add(ref)
                                        break
                                bookings_to_create.append(b)

                            created_bookings = Booking.objects.bulk_create(bookings_to_create)
                            first_booking = created_bookings[0]
                            # Ensure the object we hold has the reference
                            if not first_booking.user_facing_reference:
                                first_booking.user_facing_reference = bookings_to_create[0].user_facing_reference
                            
                            logger.info(f"[{request_id}] Bulk-created {len(created_bookings)} CONFIRMED free Bookings with references.")

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
                            # Manually generate and save reference immediately
                            first_booking.user_facing_reference = first_booking._generate_user_facing_reference()
                            first_booking.save(update_fields=['user_facing_reference'])
                            created_bookings = [first_booking]
                            logger.info(f"[{request_id}] Created CONFIRMED single free Booking: {first_booking.id}")

                        if is_guest and first_booking:
                            first_booking.cancellation_token = uuid.uuid4()
                            first_booking.save(update_fields=['cancellation_token'])

                        # --- HANDLE DISCOUNT REDEMPTION (FREE FLOW) ---
                        if discount_to_apply:
                            discount_to_apply.redeem()
                            
                            if booking_type == "Full Course":
                                booking_count = len(created_bookings)
                                if booking_count > 0:
                                    # Distribute the discount amount (which equaled the subtotal to make it free)
                                    share_discount = (calculated_discount_amount / booking_count).quantize(Decimal("0.01"))
                                    total_allocated_discount = share_discount * booking_count
                                    remainder_discount = calculated_discount_amount - total_allocated_discount
                                    
                                    applied_discounts = []
                                    for index, booking in enumerate(created_bookings):
                                        amount = share_discount
                                        if index == 0:
                                            amount += remainder_discount
                                        
                                        applied_discounts.append(AppliedDiscount(
                                            booking=booking,
                                            discount=discount_to_apply,
                                            amount_saved=amount
                                        ))
                                    AppliedDiscount.objects.bulk_create(applied_discounts)
                            else:
                                # Single Session
                                AppliedDiscount.objects.create(
                                    booking=first_booking,
                                    discount=discount_to_apply,
                                    amount_saved=calculated_discount_amount
                                )
                            logger.info(f"[{request_id}] Redeemed discount {discount_to_apply.code} for free booking(s).")

                        # Create 'Succeeded' Payment Record for 0 amount (for bookkeeping)
                        # Use a unique fake payment intent ID that can be polled
                        fake_payment_intent_id = f"pi_free_{uuid.uuid4().hex[:24]}"
                        Payment.objects.create(
                            booking=first_booking,
                            stripe_payment_intent_id=fake_payment_intent_id, 
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
                        logger.info(f"[{request_id}] Created $0.00 Payment record with fake PI: {fake_payment_intent_id}")

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

                    # Return payment_intent_id so frontend uses the same polling flow as paid bookings
                    # This ensures consistent behavior and automatic recovery if state is lost
                    response_data = {
                        "payment_intent_id": fake_payment_intent_id,
                        "client_secret": f"{fake_payment_intent_id}_secret_free",  # Fake client secret for guest auth
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
                    stripe_payment_intent_id=f"temp_{uuid.uuid4()}", 
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
                    automatic_payment_methods={"enabled": True},
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

class UpdatePaymentIntentView(APIView):
    permission_classes = [] 

    def post(self, request):
        payment_intent_id = request.data.get("payment_intent_id")
        
        if not payment_intent_id:
            return Response({"error": "Payment Intent ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                # 1. Get the pending booking
                payment = Payment.objects.select_related('booking', 'booking__contact').get(
                    stripe_payment_intent_id=payment_intent_id,
                    status='pending'
                )
                booking = payment.booking
                business = booking.schedule_instance.schedule.option.classId.businessId
                
                new_email = request.data.get("guest_email")
                new_name = request.data.get("guest_full_name")
                new_phone = request.data.get("guest_phone")
                new_notes = request.data.get("notes")
                new_participants = request.data.get("participant_details")

                # 2. Handle Contact Collision & Updates
                updated_contact = booking.contact
                if booking.contact and new_email:
                    # Check if the "Real" email already exists as a contact for this business
                    existing_contact = Contact.objects.filter(
                        business=business, 
                        email__iexact=new_email
                    ).exclude(id=booking.contact.id).first()

                    if existing_contact:
                        # CASE A: Contact exists. Switch booking to point to the EXISTING contact.
                        old_temp_contact = booking.contact
                        updated_contact = existing_contact
                        
                        # Update the existing contact with latest name/phone
                        if new_name:
                            parts = new_name.split(' ', 1)
                            existing_contact.first_name = parts[0]
                            existing_contact.last_name = parts[1] if len(parts) > 1 else ''
                        if new_phone:
                            existing_contact.phone_number = new_phone
                        existing_contact.save()
                        
                        # Clean up the placeholder contact if it was just a temp one
                        if "pending@example" in old_temp_contact.email or "pending" in old_temp_contact.email:
                            old_temp_contact.delete()
                    else:
                        # CASE B: Contact does not exist. Update the current placeholder contact.
                        if new_email: booking.contact.email = new_email
                        if new_name: 
                            parts = new_name.split(' ', 1)
                            booking.contact.first_name = parts[0]
                            booking.contact.last_name = parts[1] if len(parts) > 1 else ''
                        if new_phone:
                            booking.contact.phone_number = new_phone
                        booking.contact.save()
                        updated_contact = booking.contact

                # 3. Update Booking(s) - Handle Single vs. Course Batch
                bookings_to_update = []
                if booking.booking_group_id:
                    # If this is a course, we must update ALL bookings in the group
                    bookings_to_update = Booking.objects.filter(booking_group_id=booking.booking_group_id)
                else:
                    # Single session
                    bookings_to_update = [booking]

                # Prepare common update fields
                update_fields = {}
                if new_notes is not None:
                    update_fields['notes'] = new_notes
                if new_participants is not None:
                    update_fields['participant_details'] = new_participants
                
                # If contact changed (Case A), we must link all bookings to the new contact
                if updated_contact and updated_contact.id != booking.contact_id:
                     update_fields['contact'] = updated_contact

                # Perform the update
                if update_fields:
                    if booking.booking_group_id:
                        # For QuerySet
                        Booking.objects.filter(booking_group_id=booking.booking_group_id).update(**update_fields)
                    else:
                        # For single instance
                        for field, value in update_fields.items():
                            setattr(booking, field, value)
                        booking.save()

                # 4. Update Stripe Metadata (So webhook has backup data)
                stripe.PaymentIntent.modify(
                    payment_intent_id,
                    metadata={
                        "guest_email": new_email,
                        "guest_full_name": new_name,
                        "guest_phone": new_phone,
                        "notes": new_notes,
                    }
                )

            return Response({"status": "updated", "booking_id": booking.id}, status=status.HTTP_200_OK)

        except Payment.DoesNotExist:
            return Response({"error": "Payment intent not found or not pending"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Error updating payment intent: {e}", exc_info=True)
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
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
        Updates CourseEnrollment and all session Bookings with 1/N payout allocation.
        Redeems discounts if applicable.
        """
        try:
            with transaction.atomic():
                # Get booking group ID from metadata
                booking_group_id = payment_intent.metadata.get("booking_group_id")
                if not booking_group_id:
                    logger.error(f"[{webhook_id}] No booking_group_id in payment intent metadata")
                    raise DRFValidationError("Missing booking_group_id in payment metadata")

                # Get enrollment
                try:
                    enrollment = CourseEnrollment.objects.select_for_update().get(
                        booking_group_id=booking_group_id
                    )
                except CourseEnrollment.DoesNotExist:
                    logger.error(f"[{webhook_id}] CourseEnrollment not found for booking_group_id {booking_group_id}")
                    raise DRFValidationError(f"Course enrollment not found for booking group {booking_group_id}")

                # Update enrollment status
                enrollment.status = "active"
                enrollment.total_amount_paid = Decimal(str(payment_intent.amount_received)) / 100
                enrollment.save(update_fields=["status", "total_amount_paid"])

                # Get all bookings for this course
                bookings = list(
                    Booking.objects.select_for_update()
                    .filter(booking_group_id=booking_group_id)
                    .order_by("course_session_number")
                )

                if not bookings:
                    raise DRFValidationError(f"No bookings found for course enrollment")

                # --- 1/N Calculation Logic ---
                
                # 1. Retrieve Financials from Metadata (calculated in CreatePaymentIntentView)
                metadata = payment_intent.metadata
                subtotal_after_discount = Decimal(metadata.get("subtotal_after_discount", "0.00"))
                total_tax = Decimal(metadata.get("tax_amount", "0.00"))
                grand_total = Decimal(payment_intent.amount_received) / 100

                # 2. Calculate Business Net Revenue (Total Net Payout)
                business = enrollment.schedule.option.classId.businessId
                if metadata.get("booking_source") == "widget":
                    fee_percentage = Decimal("6.00")
                else:
                    fee_percentage = (
                        business.partner_tier.fee_percentage
                        if business.partner_tier
                        else PartnerTier.objects.get(is_default=True).fee_percentage
                    )

                service_fee_rate = fee_percentage / Decimal("100.0")
                platform_fee_amount = (subtotal_after_discount * service_fee_rate).quantize(Decimal("0.01"))
                platform_fee_tax = (platform_fee_amount * HST_RATE).quantize(Decimal("0.01"))
                
                # This is the total bucket of money the business is owed for the whole course
                business_payout_tax = total_tax - platform_fee_tax
                business_net_revenue = subtotal_after_discount - platform_fee_amount
                total_net_payout_to_business = business_net_revenue + business_payout_tax

                # 3. Calculate Share Per Booking
                booking_count = len(bookings)
                if booking_count > 0:
                    share_per_booking = (total_net_payout_to_business / booking_count).quantize(Decimal("0.01"))
                    
                    # Handle rounding remainders (e.g. 100 / 3 = 33.33, 33.33, 33.33 -> Remainder 0.01)
                    total_allocated = share_per_booking * booking_count
                    remainder = total_net_payout_to_business - total_allocated
                else:
                    share_per_booking = Decimal("0.00")
                    remainder = Decimal("0.00")

                # 4. Update Bookings
                generated_refs = set()
                
                # Calculate session price for display (customer facing amount)
                session_price = (enrollment.total_amount_paid / booking_count).quantize(Decimal("0.01"))

                for index, booking in enumerate(bookings):
                    booking.status = "confirmed"
                    booking.payment_status = "paid"
                    booking.amount_paid = session_price
                    
                    # Assign the calculated payout share
                    booking.allocated_net_payout = share_per_booking
                    
                    # Add the penny remainder to the first booking
                    if index == 0:
                        booking.allocated_net_payout += remainder

                    # Generate reference if missing
                    if not booking.user_facing_reference:
                        while True:
                            ref = booking._generate_user_facing_reference()
                            if ref not in generated_refs:
                                booking.user_facing_reference = ref
                                generated_refs.add(ref)
                                break

                Booking.objects.bulk_update(
                    bookings,
                    [
                        "status",
                        "payment_status",
                        "amount_paid",
                        "user_facing_reference",
                        "allocated_net_payout",
                    ],
                )

                # --- Handle Discount Redemption ---
                applied_discount_id = metadata.get("applied_discount_id")
                total_discount_amount = Decimal(metadata.get("discount_amount", "0.00"))

                if applied_discount_id:
                    try:
                        discount = Discount.objects.select_for_update().get(pk=applied_discount_id)
                        discount.redeem()  # Increments usage_count atomically

                        # Distribute the applied discount amount across bookings for record-keeping
                        if booking_count > 0:
                            share_discount = (total_discount_amount / booking_count).quantize(Decimal("0.01"))
                            total_allocated_discount = share_discount * booking_count
                            remainder_discount = total_discount_amount - total_allocated_discount
                            
                            applied_discounts = []
                            for index, booking in enumerate(bookings):
                                amount = share_discount
                                if index == 0:
                                    amount += remainder_discount
                                
                                applied_discounts.append(AppliedDiscount(
                                    booking=booking,
                                    discount=discount,
                                    amount_saved=amount
                                ))
                            
                            AppliedDiscount.objects.bulk_create(applied_discounts)
                            logger.info(f"[{webhook_id}] Redeemed discount {discount.code} for course (Group: {booking_group_id}).")

                    except Discount.DoesNotExist:
                        logger.warning(f"[{webhook_id}] Discount ID {applied_discount_id} found in metadata but not in DB.")

                # --- Payment Record Update (For Bookkeeping) ---
                try:
                    payment_record = Payment.objects.select_for_update().get(
                        stripe_payment_intent_id=payment_intent.id
                    )
                except Payment.DoesNotExist:
                    raise DRFValidationError("Payment record not found.")

                payment_record.status = "succeeded"
                payment_record.amount = grand_total
                payment_record.tax_amount = total_tax
                payment_record.platform_fee_amount = platform_fee_amount
                payment_record.platform_fee_tax = platform_fee_tax
                payment_record.net_payout_amount = total_net_payout_to_business # Total for the whole course
                payment_record.metadata = {"original_stripe_metadata": dict(metadata)}
                payment_record.stripe_charge_id = payment_intent.latest_charge
                
                # Stripe Charge Details
                charge_details = (
                    stripe.Charge.retrieve(payment_intent.latest_charge)
                    if payment_intent.latest_charge
                    else None
                )
                if charge_details:
                    payment_record.receipt_url = charge_details.receipt_url
                    if charge_details.payment_method_details.card:
                        payment_record.card_brand = charge_details.payment_method_details.card.brand
                        payment_record.card_last4 = charge_details.payment_method_details.card.last4

                payment_record.save()

                # --- Emails & Notifications (Existing logic) ---
                first_booking = bookings[0]
                recipient_user = first_booking.user
                recipient_contact = first_booking.contact

                if recipient_user:
                    send_booking_confirmation_email(recipient_user, first_booking)
                elif recipient_contact:
                    send_booking_confirmation_email(recipient_contact, first_booking)

                if business.newBookingNotification:
                    recipients = {business.owner}
                    staff_to_notify = BusinessStaff.objects.filter(
                        business=business,
                        status="accepted",
                        role__permissions__codename="receive_booking_notifications",
                    ).select_related("user")

                    for staff in staff_to_notify:
                        if staff.user: recipients.add(staff.user)

                    for recipient in recipients:
                        if recipient and recipient.email:
                            send_business_new_booking_email(recipient, first_booking)

                logger.info(f"[{webhook_id}] 1/N Payout processed. Total Net: {total_net_payout_to_business}, Per Booking: {share_per_booking}")
                return {"message": "Course payment processed successfully"}

        except Exception as e:
            logger.error(f"[{webhook_id}] Error processing course payment success: {e}", exc_info=True)
            raise

    def handle_successful_payment(self, payment_intent, webhook_id):
        if Payment.objects.filter(
            stripe_payment_intent_id=payment_intent.id, status="succeeded"
        ).exists():
            logger.warning(
                f"[{webhook_id}] DUPLICATE: PI {payment_intent.id} already processed."
            )
            return {"message": "Already processed"}

        # FIX: Use correct metadata key 'booking_type' as sent by CreatePaymentIntentView
        enrollment_type = payment_intent.metadata.get("booking_type")

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

            if not pending_booking.user_facing_reference:
                pending_booking.user_facing_reference = pending_booking._generate_user_facing_reference()

            if pending_booking.contact and not pending_booking.user:
                pending_booking.cancellation_token = uuid.uuid4()
                logger.info(
                    f"[{webhook_id}] Generated cancellation token for guest booking {pending_booking.id}"
                )

            pending_booking.save()
            logger.info(f"[{webhook_id}] Booking {pending_booking.id} confirmed.")

            # --- Handle Discount Redemption (Single Session) ---
            applied_discount_id = metadata.get("applied_discount_id")
            discount_amount = Decimal(metadata.get("discount_amount", "0.00"))

            if applied_discount_id:
                try:
                    discount = Discount.objects.select_for_update().get(pk=applied_discount_id)
                    discount.redeem() # Increments usage_count atomically
                    
                    AppliedDiscount.objects.create(
                        booking=pending_booking,
                        discount=discount,
                        amount_saved=discount_amount
                    )
                    logger.info(f"[{webhook_id}] Redeemed discount {discount.code} for booking {pending_booking.id}")
                except Discount.DoesNotExist:
                    logger.warning(f"[{webhook_id}] Discount {applied_discount_id} not found during webhook processing.")

            # --- CALCULATE FEES AND UPDATE PAYMENT RECORD ---
            grand_total = Decimal(payment_intent.amount_received) / 100
            total_tax = Decimal(metadata.get("tax_amount", "0.00"))
            subtotal_after_discount = Decimal(
                metadata.get("subtotal_after_discount", "0.00")
            )
            business = initial_instance.schedule.option.classId.businessId

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
    
class BookingStatusByPaymentIntentView(APIView):
    # Allow unauthenticated access, as guests will use this endpoint.
    permission_classes = []

    def get(self, request, payment_intent_id):
        if not payment_intent_id:
            return Response(
                {"error": "Payment Intent ID is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # For guests, we require the client_secret as a temporary auth token.
        is_guest = not request.user or not request.user.is_authenticated
        client_secret = request.query_params.get("client_secret")

        if is_guest and not client_secret:
            logger.warning(
                f"Guest status check for PI {payment_intent_id} failed: missing client_secret."
            )
            return Response(
                {"error": "Authorization required."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            payment = (
                Payment.objects.select_related("booking")
                .filter(stripe_payment_intent_id=payment_intent_id)
                .first()
            )

            if not payment:
                logger.info(
                    f"Booking status check for PI {payment_intent_id}: Payment record not found yet (webhook might be pending)."
                )
                return Response(
                    {
                        "status": "pending_webhook",
                        "message": "Booking confirmation is processing.",
                    },
                    status=status.HTTP_202_ACCEPTED,
                )

            # Security Check
            is_authorized = False
            if not is_guest:
                # Logged-in user check
                if payment.booking and payment.booking.user == request.user:
                    is_authorized = True
            else:
                # Guest check using client_secret
                try:
                    retrieved_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
                    if retrieved_intent.client_secret == client_secret:
                        is_authorized = True
                except stripe.error.StripeError as e:
                    logger.error(
                        f"Stripe API error checking client_secret for PI {payment_intent_id}: {e}"
                    )

            if not is_authorized:
                logger.warning(
                    f"User/Guest attempted to access booking status for PI {payment_intent_id} without authorization."
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
                        "participant_details": booking.participant_details,
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
                )
            else:
                logger.info(
                    f"Booking status check for PI {payment_intent_id}: Payment status is '{payment.status}'."
                )
                return Response(
                    {
                        "status": "processing",
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