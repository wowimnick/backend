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
from django.db import IntegrityError as DjangoIntegrityError
from decimal import Decimal
import stripe
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q, F, Sum
from django.db.models.functions import Coalesce

# Imported Models
from quickstart.models import (
    BusinessInfo,
    BusinessAddonSubscription,
    CourseEnrollment,
    CustomUser,
    Booking,
    Discount,
    AppliedDiscount,
    GlobalDiscount,
    AppliedGlobalDiscount,
    PartnerTier,
    Payment,
    ScheduleInstance,
    Schedule,
    Contact,
    BusinessStaff,
    FirstPurchaseGiftCardSent,
    GiftCard,
    GiftCardTransaction,
    WidgetSubscription,
)
from quickstart.models import ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
from quickstart.serializers.public.public_booking_serializers import (
    BookingCreateSerializer,
)

from quickstart.utils.email_utils import (
    send_booking_confirmation_email,
    send_business_new_booking_email,
    send_gift_card_email,
    send_super_admin_booking_created_email,
    send_widget_subscription_payment_failed_email,
    _is_placeholder_booker_email,
    _is_placeholder_phone,
    is_placeholder_guest_contact,
)
from quickstart.utils.sms_utils import normalize_phone_for_sns, business_sms_enabled
from quickstart.tasks.notification_tasks import send_sms_task

import logging

from quickstart.utils.revalidation import (
    trigger_nextjs_revalidation,
    trigger_multiple_revalidations,
)
from quickstart.utils.meta_capi import send_purchase_event_for_booking

logger = logging.getLogger(__name__)


def _revalidate_for_booking(booking):
    """Trigger Next.js cache revalidation for pages affected by a booking (create/cancel/update)."""
    if not booking:
        return
    try:
        option = booking.schedule_instance.schedule.option
        class_obj = option.classId
        business = class_obj.businessId
        class_slug = getattr(class_obj, "slug", None) or str(class_obj.classId)
        business_slug = getattr(business, "slug", None) or str(business.businessId)
        tags = [
            "classes-search",
            "homepage-content",
            "homepage-classes",
            "classes",
            f"class-{class_slug}",
            f"business-{business_slug}",
        ]
        trigger_multiple_revalidations(tags=tags)
        if class_slug:
            trigger_nextjs_revalidation(path=f"/classes/{class_slug}")
    except Exception as e:
        logger.warning("Revalidation for booking failed: %s", e)

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
            guest_full_name = (request.data.get("guest_full_name") or "").strip()
            guest_phone = request.data.get("guest_phone")
            # Fallback: derive booker name from first participant if guest_full_name not provided (e.g. frontend uses "Who's Coming?" only)
            if not guest_full_name:
                participant_details = request.data.get("participant_details") or []
                if isinstance(participant_details, list) and participant_details:
                    first_detail = participant_details[0]
                    if isinstance(first_detail, dict) and first_detail.get("name"):
                        guest_full_name = str(first_detail.get("name", "")).strip()
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

        # 1. Capture Gift Card Code
        gift_card_code = request.data.get("gift_card_code")

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
            # When frontend only sends booker name, participant_details may be empty or wrong length.
            # Normalize to one booker name per participant so emails/dashboards have a consistent shape.
            if not participant_details or len(participant_details) != participants:
                if is_guest:
                    booker_name = (request.data.get("guest_full_name") or "").strip()
                    if not booker_name and participant_details and isinstance(participant_details[0], dict):
                        booker_name = (participant_details[0].get("name") or "").strip()
                    booker_name = booker_name or "Guest"
                else:
                    booker_name = (
                        f"{getattr(request.user, 'first_name', '') or ''} {getattr(request.user, 'last_name', '') or ''}"
                    ).strip() or "Guest"
                participant_details = [{"name": booker_name} for _ in range(participants)]

            # --- FETCH ALL SIBLING SCHEDULES FOR COURSES ---
            all_instances = []

            if booking_type == "Full Course":
                # If Full Course, we must find ALL schedules that match this group
                representative_schedule = instance.schedule

                sibling_schedules = Schedule.objects.filter(
                    option=representative_schedule.option,
                    start_date=representative_schedule.start_date,
                    end_date=representative_schedule.end_date,
                    time=representative_schedule.time,
                    price=representative_schedule.price,
                )

                # Fetch ALL future instances for ALL these schedules
                all_instances = list(
                    ScheduleInstance.objects.filter(
                        schedule__in=sibling_schedules,
                        status="scheduled",
                        date__gte=timezone.now().date(),
                    ).order_by("date")
                )

                if not all_instances:
                    return Response(
                        {"error": "No upcoming sessions found for this course."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # RE-VALIDATE CAPACITY for siblings
                for group_inst in all_instances:
                    if not group_inst.can_accommodate(participants):
                        return Response(
                            {
                                "error": f"Session on {group_inst.date} does not have enough capacity."
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
            else:
                # Single Session - just use the validated instance
                all_instances = [instance]

            # --- Pricing and Discount Calculation ---
            if booking_type == "Full Course":
                # For a course, the price is fixed on the Schedule per student, not per session.
                subtotal = Decimal(instance.schedule.price) * participants
            else:
                # For single sessions, sum the price of each instance
                subtotal = (
                    sum([Decimal(inst.price) for inst in all_instances]) * participants
                )

            final_amount = subtotal
            discount_to_apply = None
            calculated_discount_amount = Decimal("0.00")

            # 1. APPLY BUSINESS COUPON DISCOUNT
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

            # Subtotal after business discount = basis for business payout (business never loses from global discount)
            subtotal_for_payout = final_amount

            # 2. APPLY GLOBAL DISCOUNT (platform absorbs; business payout unchanged)
            global_discount_to_apply = None
            global_discount_amount = Decimal("0.00")
            now = timezone.now()
            active_global = (
                GlobalDiscount.objects.filter(is_active=True)
                .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=now))
                .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=now))
                .filter(Q(usage_limit__isnull=True) | Q(usage_count__lt=F("usage_limit")))
                .order_by("-created_at")
                .first()
            )
            if active_global and subtotal_for_payout > 0:
                is_valid_global, _ = active_global.is_valid_for_amount(subtotal_for_payout)
                if is_valid_global:
                    global_discount_to_apply = active_global
                    if active_global.discount_type == "percentage":
                        global_discount_amount = (
                            subtotal_for_payout
                            * (active_global.value / Decimal(100))
                        ).quantize(Decimal("0.01"))
                    else:
                        global_discount_amount = min(
                            active_global.value, subtotal_for_payout
                        )
                    final_amount = subtotal_for_payout - global_discount_amount

            subtotal_after_discount = final_amount
            tax_amount = (subtotal_after_discount * HST_RATE).quantize(Decimal("0.01"))
            grand_total = subtotal_after_discount + tax_amount

            # 2. APPLY GIFT CARD (Step C)
            amount_covered_by_gc = Decimal("0.00")
            gift_card_obj = None

            if gift_card_code:
                try:
                    with transaction.atomic():
                        # Lock the GC row to prevent race conditions
                        gift_card_obj = GiftCard.objects.select_for_update().get(
                            code__iexact=gift_card_code, is_active=True
                        )

                        if gift_card_obj.current_balance > 0:
                            if gift_card_obj.current_balance >= grand_total:
                                amount_covered_by_gc = grand_total
                            else:
                                amount_covered_by_gc = gift_card_obj.current_balance
                except GiftCard.DoesNotExist:
                    return Response({"error": "Invalid Gift Card Code"}, status=400)

            # Final amount Stripe needs to charge
            amount_remaining = grand_total - amount_covered_by_gc
            total_amount_for_stripe_cents = int(amount_remaining * 100)

            # --- Check for Stripe Minimum (only if price > 0) ---
            if 0 < total_amount_for_stripe_cents < 50:
                return Response(
                    {
                        "error": "The remaining amount is too low to process. Please add more items or pay fully with card/gift card."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # ==========================================
            #  FREE / FULLY COVERED BY GC FLOW
            # ==========================================
            if total_amount_for_stripe_cents == 0:
                logger.info(
                    f"[{request_id}] Processing PAID booking (Total via Stripe: $0.00). Fully covered or Free."
                )

                first_booking = None
                booking_group_id = None

                try:
                    with transaction.atomic():
                        # --- 2a. DEDUCT GIFT CARD BALANCE IMMEDIATELY ---
                        if amount_covered_by_gc > 0 and gift_card_obj:
                            gift_card_obj.current_balance -= amount_covered_by_gc
                            gift_card_obj.save()
                            logger.info(
                                f"[{request_id}] Deducted ${amount_covered_by_gc} from GC {gift_card_obj.code} immediately."
                            )

                        # --- Create Bookings ---
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
                                total_amount_paid=grand_total,  # Record full value
                                cancellation_policy=option.cancellationPolicy,
                                cancellation_custom_hours=option.cancellationCustomHours,
                                cancellation_refund_percentage=option.cancellationRefundPercentage,
                            )
                            logger.info(
                                f"[{request_id}] Created ACTIVE CourseEnrollment: {enrollment.id}"
                            )

                            bookings_to_create = []
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
                                    amount_paid=Decimal(
                                        "0.00"
                                    ),  # Split happens later in reporting, or we can split now
                                    status="confirmed",  # Directly confirmed
                                    payment_status="paid",
                                    cancellation_policy=enrollment.cancellation_policy,
                                    cancellation_custom_hours=enrollment.cancellation_custom_hours,
                                    cancellation_refund_percentage=enrollment.cancellation_refund_percentage,
                                )
                                while True:
                                    ref = b._generate_user_facing_reference()
                                    if ref not in generated_refs:
                                        b.user_facing_reference = ref
                                        generated_refs.add(ref)
                                        break
                                bookings_to_create.append(b)

                            created_bookings = Booking.objects.bulk_create(
                                bookings_to_create
                            )
                            first_booking = created_bookings[0]
                            if not first_booking.user_facing_reference:
                                first_booking.user_facing_reference = (
                                    bookings_to_create[0].user_facing_reference
                                )

                            logger.info(
                                f"[{request_id}] Bulk-created {len(created_bookings)} CONFIRMED Bookings."
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
                                status="confirmed",  # Directly confirmed
                                payment_status="paid",
                                enrollment_type="Single Session",
                                cancellation_policy=option.cancellationPolicy,
                                cancellation_refund_percentage=option.cancellationRefundPercentage,
                                cancellation_custom_hours=option.cancellationCustomHours,
                            )
                            first_booking.user_facing_reference = (
                                first_booking._generate_user_facing_reference()
                            )
                            first_booking.save(update_fields=["user_facing_reference"])
                            created_bookings = [first_booking]
                            logger.info(
                                f"[{request_id}] Created CONFIRMED single Booking: {first_booking.id}"
                            )

                        if is_guest and first_booking:
                            first_booking.cancellation_token = uuid.uuid4()
                            first_booking.save(update_fields=["cancellation_token"])

                        # --- HANDLE COUPON REDEMPTION (FREE FLOW) ---
                        if discount_to_apply:
                            discount_to_apply.redeem()

                            if booking_type == "Full Course":
                                booking_count = len(created_bookings)
                                if booking_count > 0:
                                    share_discount = (
                                        calculated_discount_amount / booking_count
                                    ).quantize(Decimal("0.01"))
                                    total_allocated_discount = (
                                        share_discount * booking_count
                                    )
                                    remainder_discount = (
                                        calculated_discount_amount
                                        - total_allocated_discount
                                    )

                                    applied_discounts = []
                                    for index, booking in enumerate(created_bookings):
                                        amount = share_discount
                                        if index == 0:
                                            amount += remainder_discount

                                        applied_discounts.append(
                                            AppliedDiscount(
                                                booking=booking,
                                                discount=discount_to_apply,
                                                amount_saved=amount,
                                            )
                                        )
                                    AppliedDiscount.objects.bulk_create(
                                        applied_discounts
                                    )
                            else:
                                AppliedDiscount.objects.create(
                                    booking=first_booking,
                                    discount=discount_to_apply,
                                    amount_saved=calculated_discount_amount,
                                )

                        # --- HANDLE GLOBAL DISCOUNT REDEMPTION (FREE FLOW) ---
                        if global_discount_to_apply and global_discount_amount > 0:
                            global_discount_to_apply.redeem()
                            if booking_type == "Full Course":
                                booking_count = len(created_bookings)
                                if booking_count > 0:
                                    share_g = (
                                        global_discount_amount / booking_count
                                    ).quantize(Decimal("0.01"))
                                    total_alloc_g = share_g * booking_count
                                    remainder_g = global_discount_amount - total_alloc_g
                                    for index, b in enumerate(created_bookings):
                                        amt = share_g + (remainder_g if index == 0 else Decimal("0"))
                                        AppliedGlobalDiscount.objects.create(
                                            booking=b,
                                            global_discount=global_discount_to_apply,
                                            amount_saved=amt,
                                        )
                            else:
                                AppliedGlobalDiscount.objects.create(
                                    booking=first_booking,
                                    global_discount=global_discount_to_apply,
                                    amount_saved=global_discount_amount,
                                )

                        # --- LOG GIFT CARD TRANSACTION ---
                        if amount_covered_by_gc > 0 and gift_card_obj:
                            GiftCardTransaction.objects.create(
                                gift_card=gift_card_obj,
                                booking=first_booking,
                                amount=-amount_covered_by_gc,
                                balance_after=gift_card_obj.current_balance,
                                transaction_type="redemption",
                            )

                        # Create 'Succeeded' Payment Record (Internal)
                        Payment.objects.create(
                            booking=first_booking,
                            stripe_payment_intent_id=f"internal_{uuid.uuid4()}",
                            amount=grand_total,
                            tax_amount=tax_amount,
                            currency="CAD",
                            status="succeeded",
                            metadata={
                                "is_free": (grand_total == 0),
                                "paid_via_giftcard": (amount_covered_by_gc > 0),
                                "gift_card_code": (
                                    gift_card_code if gift_card_code else None
                                ),
                                "notes": notes,
                                "applied_discount_id": (
                                    str(discount_to_apply.id)
                                    if discount_to_apply
                                    else None
                                ),
                            },
                        )
                        logger.info(f"[{request_id}] Created internal Payment record.")

                    # --- Emails ---
                    recipient_user = request.user if not is_guest else None
                    recipient_contact = guest_contact if is_guest else None

                    if recipient_user:
                        send_booking_confirmation_email(recipient_user, first_booking)
                    elif recipient_contact:
                        send_booking_confirmation_email(
                            recipient_contact, first_booking
                        )

                    if business_sms_enabled(business):
                        booker = recipient_user or recipient_contact
                        phone = getattr(booker, "phone_number", None) if booker else None
                        if not phone and first_booking.metadata:
                            phone = first_booking.metadata.get("guest_phone") or ""
                        normalized = normalize_phone_for_sns(phone or "")
                        if normalized and first_booking.schedule_instance:
                            class_title = getattr(option.classId, "title", "Class")
                            date_str = first_booking.schedule_instance.date.strftime("%b %d")
                            t = first_booking.schedule_instance.time
                            time_str = t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else str(t)
                            business_name = getattr(business, "businessName", "") or "ClassEasily"
                            sms_msg = f"You're in! {class_title} is on {date_str} at {time_str}.\n\nAdd it to your calendar — we'll send a reminder the day before.\n\n— {business_name}"
                            try:
                                send_sms_task.delay(normalized, sms_msg)
                            except Exception as sms_e:
                                logger.warning("Booking confirmation SMS failed: %s", sms_e)

                    if business.newBookingNotification:
                        recipients = {business.owner}
                        staff_to_notify = BusinessStaff.objects.filter(
                            business=business,
                            status="accepted",
                            role__permissions__codename="receive_booking_notifications",
                        ).select_related("user")

                        for staff in staff_to_notify:
                            if staff.user:
                                recipients.add(staff.user)

                        for r in recipients:
                            if r and r.email:
                                send_business_new_booking_email(r, first_booking)

                        if business_sms_enabled(business) and first_booking.schedule_instance:
                            class_title = getattr(option.classId, "title", "Class")
                            date_str = first_booking.schedule_instance.date.strftime("%b %d")
                            t = first_booking.schedule_instance.time
                            time_str = t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else str(t)
                            booker = recipient_user or recipient_contact
                            if booker and hasattr(booker, "first_name") and hasattr(booker, "last_name"):
                                booker_name = f"{getattr(booker, 'first_name', '')} {getattr(booker, 'last_name', '')}".strip() or getattr(booker, "email", "A customer")
                            elif booker:
                                booker_name = getattr(booker, "email", "A customer") or "A customer"
                            else:
                                booker_name = "A customer"
                            sms_msg = f"New booking: {class_title} on {date_str} at {time_str}.\n\nBooked by {booker_name}. Check your dashboard for details.\n\n— ClassEasily"
                            for r in recipients:
                                if not r:
                                    continue
                                phone = getattr(r, "phone_number", None) or ""
                                normalized = normalize_phone_for_sns(phone)
                                if normalized:
                                    try:
                                        send_sms_task.delay(normalized, sms_msg)
                                    except Exception as sms_e:
                                        logger.warning("New booking SMS failed for %s: %s", getattr(r, "email", ""), sms_e)

                        send_super_admin_booking_created_email(first_booking)

                    response_data = {
                        "booking_id": first_booking.id,
                        "user_facing_reference": first_booking.user_facing_reference,
                        "booking_group_id": (
                            str(booking_group_id) if booking_group_id else None
                        ),
                        "participant_details": participant_details,
                        "status": "confirmed",
                        "message": "Booking confirmed successfully.",
                    }
                    # Meta CAPI: server-side Purchase with deduplication (event_id = booking_id)
                    try:
                        class_obj = option.classId if option else None
                        send_purchase_event_for_booking(
                            first_booking,
                            value=float(grand_total),
                            currency="CAD",
                            content_ids=(
                                [str(class_obj.classId)]
                                if class_obj and getattr(class_obj, "classId", None)
                                else None
                            ),
                            content_name=(
                                getattr(class_obj, "title", None) if class_obj else None
                            ),
                            num_items=participants,
                            request=request,
                        )
                    except Exception as capi_err:
                        logger.warning(
                            "[%s] Meta CAPI send failed (non-fatal): %s",
                            request_id,
                            capi_err,
                        )
                    _revalidate_for_booking(first_booking)
                    return Response(response_data)

                except Exception as e:
                    logger.error(
                        f"[{request_id}] Error processing free/GC booking: {str(e)}",
                        exc_info=True,
                    )
                    return Response(
                        {"error": "An error occurred while processing the booking."},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    )

            # ==========================================
            #  PAID BOOKING FLOW
            #  - Single Session: No spot holding; create Stripe intent only; booking created in webhook.
            #  - Full Course: Keep pending enrollment/bookings (course webhook expects them).
            # ==========================================
            logger.info(
                "[%s] Paid booking flow: building metadata booking_type=%s participants=%s",
                request_id,
                booking_type,
                participants,
            )
            currency_code = getattr(settings, "STRIPE_CURRENCY", "cad")

            # Common metadata for webhook
            metadata = {
                "participants": str(participants),
                "booking_type": booking_type,
                "notes": notes or "",
                "applied_discount_id": (
                    str(discount_to_apply.id) if discount_to_apply else None
                ),
                "discount_amount": str(calculated_discount_amount),
                "subtotal_for_payout": str(subtotal_for_payout),
                "subtotal_after_discount": str(subtotal_after_discount),
                "global_discount_id": (
                    str(global_discount_to_apply.id)
                    if global_discount_to_apply
                    else None
                ),
                "global_discount_amount": str(global_discount_amount),
                "tax_amount": str(tax_amount),
                "is_guest": str(is_guest),
                "gift_card_code": gift_card_code if gift_card_code else "",
                "gift_card_amount_to_deduct": str(amount_covered_by_gc),
            }
            if is_guest:
                metadata["guest_contact_id"] = str(guest_contact.id)
            else:
                metadata["user_id"] = str(request.user.userId)
            if request.data.get("meta_fbc"):
                metadata["meta_fbc"] = request.data.get("meta_fbc")
            if request.data.get("meta_fbp"):
                metadata["meta_fbp"] = request.data.get("meta_fbp")

            first_booking = None
            booking_group_id = None
            pending_payment = None

            if booking_type == "Full Course":
                # Course: create pending enrollment + bookings so course webhook can confirm them
                logger.info("[%s] Creating pending course enrollment and bookings", request_id)
                with transaction.atomic():
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
                    pending_payment = Payment.objects.create(
                        booking=first_booking,
                        stripe_payment_intent_id=f"temp_{uuid.uuid4()}",
                        amount=grand_total,
                        tax_amount=tax_amount,
                        currency=currency_code.upper(),
                        status="pending",
                    )
                    metadata["booking_group_id"] = str(booking_group_id)
                    metadata["payment_db_id"] = str(pending_payment.id)
                    metadata["first_booking_db_id"] = str(first_booking.id)
            else:
                # Single Session: no pending records; webhook will create booking from metadata
                metadata["schedule_instance_id"] = str(instance.id)
                metadata["participant_details_json"] = json.dumps(participant_details)
                logger.info("[%s] Single session: metadata ready, creating PaymentIntent (no DB hold)", request_id)

            try:
                intent = stripe.PaymentIntent.create(
                    amount=total_amount_for_stripe_cents,
                    currency=currency_code.lower(),
                    automatic_payment_methods={"enabled": True},
                    metadata={k: v for k, v in metadata.items() if v is not None},
                )
                if pending_payment:
                    pending_payment.stripe_payment_intent_id = intent.id
                    pending_payment.save(update_fields=["stripe_payment_intent_id"])
                    logger.info(
                        f"[{request_id}] Created Stripe PaymentIntent (course): {intent.id}"
                    )
                else:
                    logger.info(
                        f"[{request_id}] Created Stripe PaymentIntent (single, no hold): {intent.id}"
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
            except stripe.error.StripeError as e:
                logger.error(
                    f"[{request_id}] Stripe error: {e}", exc_info=True
                )
                if pending_payment and first_booking:
                    with transaction.atomic():
                        if booking_group_id:
                            CourseEnrollment.objects.filter(
                                booking_group_id=booking_group_id
                            ).delete()
                            Booking.objects.filter(
                                booking_group_id=booking_group_id
                            ).delete()
                        else:
                            first_booking.delete()
                        pending_payment.delete()
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


class CheckSlotAvailabilityView(APIView):
    """
    Check if a slot still has enough capacity for the requested participants.
    Used for polling (e.g. every minute) on checkout to show "no longer available" without holding.
    """
    permission_classes = []

    def get(self, request):
        instance_id = request.query_params.get("instance_id")
        participants_str = request.query_params.get("participants", "1")
        if not instance_id:
            return Response(
                {"error": "instance_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            participants = int(participants_str)
            if participants < 1:
                participants = 1
        except (ValueError, TypeError):
            participants = 1
        try:
            instance = ScheduleInstance.objects.get(pk=instance_id)
        except (ValueError, ScheduleInstance.DoesNotExist):
            return Response(
                {"available": False, "available_spots": 0},
                status=status.HTTP_200_OK,
            )
        available_spots = instance.available_spots
        available = available_spots >= participants
        return Response(
            {"available": available, "available_spots": available_spots},
            status=status.HTTP_200_OK,
        )


class UpdatePaymentIntentView(APIView):
    permission_classes = []

    def post(self, request):
        update_id = str(uuid.uuid4())[:8]
        logger.info("[%s] UpdatePaymentIntentView START", update_id)
        logger.info(
            "[%s] UpdatePaymentIntent REQUEST data: payment_intent_id=%s guest_email=%s guest_full_name=%s guest_phone=%s",
            update_id,
            request.data.get("payment_intent_id"),
            request.data.get("guest_email"),
            request.data.get("guest_full_name"),
            request.data.get("guest_phone"),
        )

        payment_intent_id = request.data.get("payment_intent_id")

        if not payment_intent_id:
            logger.warning("[%s] UpdatePaymentIntent: missing payment_intent_id", update_id)
            return Response(
                {"error": "Payment Intent ID is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_email = request.data.get("guest_email")
        new_name = (request.data.get("guest_full_name") or "").strip()
        new_phone = request.data.get("guest_phone")
        new_notes = request.data.get("notes")
        new_participants = request.data.get("participant_details")

        # Safeguard: never overwrite metadata with placeholder/mock guest details
        if new_email and _is_placeholder_booker_email(new_email):
            return Response(
                {"error": "Please enter your real email address."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if new_phone and _is_placeholder_phone(new_phone):
            return Response(
                {"error": "Please enter your real phone number."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if new_name and (new_name.strip().lower() == "guest" or not new_name.strip()):
            return Response(
                {"error": "Please enter your full name."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not new_name and new_participants and isinstance(new_participants, list) and new_participants:
            first_p = new_participants[0]
            if isinstance(first_p, dict) and first_p.get("name"):
                new_name = str(first_p.get("name", "")).strip()

        # Stripe replaces entire metadata on modify; merge guest fields into existing.
        # Do not overwrite existing non-empty metadata with empty values (e.g. second
        # update_intent from Apple Pay with stale/empty form would otherwise wipe good data).
        guest_updates = {
            "guest_email": new_email,
            "guest_full_name": new_name,
            "guest_phone": new_phone,
            "notes": new_notes,
        }

        def merge_and_modify_metadata():
            intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            merged = dict(intent.metadata or {})
            for k, v in guest_updates.items():
                if k == "notes":
                    merged[k] = v if v is not None else ""
                else:
                    # Do not overwrite existing guest_* with empty; prevents a second
                    # update_intent (e.g. from Apple Pay) with empty form from wiping good data.
                    if v is not None and str(v).strip():
                        merged[k] = v
                    elif k not in merged:
                        merged[k] = v if v is not None else ""
            stripe.PaymentIntent.modify(
                payment_intent_id,
                metadata=merged,
            )

        try:
            payment = Payment.objects.select_related(
                "booking", "booking__contact"
            ).get(stripe_payment_intent_id=payment_intent_id, status="pending")
            logger.info("[%s] UpdatePaymentIntent: found pending payment id=%s booking_id=%s", update_id, payment.id, payment.booking_id)
        except Payment.DoesNotExist:
            # No pending Payment = single-session flow. Just update Stripe metadata and return success.
            logger.info("[%s] UpdatePaymentIntent: no pending payment (single-session flow), updating Stripe metadata only", update_id)
            logger.info(
                "[%s] UpdatePaymentIntent WRITING to Stripe: guest_email=%s guest_full_name=%s guest_phone=%s",
                update_id,
                new_email,
                new_name,
                new_phone,
            )
            try:
                merge_and_modify_metadata()
                intent_after = stripe.PaymentIntent.retrieve(payment_intent_id)
                logger.info(
                    "[%s] UpdatePaymentIntent: Stripe metadata AFTER update for PI %s: guest_email=%s guest_full_name=%s guest_phone=%s",
                    update_id,
                    payment_intent_id,
                    (intent_after.metadata or {}).get("guest_email"),
                    (intent_after.metadata or {}).get("guest_full_name"),
                    (intent_after.metadata or {}).get("guest_phone"),
                )
                logger.info("[%s] UpdatePaymentIntent: Stripe metadata updated for PI %s", update_id, payment_intent_id)
                return Response(
                    {"status": "updated", "metadata_only": True},
                    status=status.HTTP_200_OK,
                )
            except stripe.error.StripeError as e:
                logger.warning("[%s] UpdatePaymentIntent: Stripe modify failed for %s: %s", update_id, payment_intent_id, e)
                return Response(
                    {"error": "Could not update payment intent."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Pending Payment exists (course or single with hold): update DB and Stripe.
        try:
            with transaction.atomic():
                booking = payment.booking
                business = booking.schedule_instance.schedule.option.classId.businessId

                # 2. Handle Contact Collision & Updates
                updated_contact = booking.contact
                if booking.contact and new_email:
                    existing_contact = (
                        Contact.objects.filter(
                            business=business, email__iexact=new_email
                        )
                        .exclude(id=booking.contact.id)
                        .first()
                    )

                    if existing_contact:
                        old_temp_contact = booking.contact
                        updated_contact = existing_contact
                        if new_name:
                            parts = new_name.split(" ", 1)
                            existing_contact.first_name = parts[0]
                            existing_contact.last_name = (
                                parts[1] if len(parts) > 1 else ""
                            )
                        if new_phone:
                            existing_contact.phone_number = new_phone
                        existing_contact.save()
                        if (
                            "pending@example" in old_temp_contact.email
                            or "pending" in old_temp_contact.email
                        ):
                            old_temp_contact.delete()
                    else:
                        if new_email:
                            booking.contact.email = new_email
                        if new_name:
                            parts = new_name.split(" ", 1)
                            booking.contact.first_name = parts[0]
                            booking.contact.last_name = (
                                parts[1] if len(parts) > 1 else ""
                            )
                        if new_phone:
                            booking.contact.phone_number = new_phone
                        booking.contact.save()
                        updated_contact = booking.contact

                # 3. Update Booking(s)
                bookings_to_update = []
                if booking.booking_group_id:
                    bookings_to_update = Booking.objects.filter(
                        booking_group_id=booking.booking_group_id
                    )
                else:
                    bookings_to_update = [booking]

                update_fields = {}
                if new_notes is not None:
                    update_fields["notes"] = new_notes
                if new_participants is not None:
                    count = booking.participants or 1
                    if not new_participants or len(new_participants) != count:
                        booker_name = new_name.strip() if new_name else "Guest"
                        new_participants = [{"name": booker_name} for _ in range(count)]
                    update_fields["participant_details"] = new_participants
                if updated_contact and updated_contact.id != booking.contact_id:
                    update_fields["contact"] = updated_contact

                if update_fields:
                    if booking.booking_group_id:
                        Booking.objects.filter(
                            booking_group_id=booking.booking_group_id
                        ).update(**update_fields)
                    else:
                        for field, value in update_fields.items():
                            setattr(booking, field, value)
                        booking.save()

                # 4. Update Stripe metadata (merge so we don't wipe schedule_instance_id, etc.)
                merge_and_modify_metadata()

            logger.info("[%s] UpdatePaymentIntent: DB and Stripe updated for booking_id=%s", update_id, booking.id)
            return Response(
                {"status": "updated", "booking_id": booking.id},
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.error("[%s] UpdatePaymentIntent error: %s", update_id, e, exc_info=True)
            return Response(
                {"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


def try_send_first_purchase_gift_card(metadata, grand_total, payment_intent_id=None):
    """
    If this is the customer's first completed purchase and grand_total meets the tier,
    create a gift card, email it, and record that we sent. Card is redeemable on next purchase only.
    Tiers by total (after tax): $75–$99 → $10, $100–$199 → $15, $200+ → $20.
    """
    try:
        grand_total = Decimal(str(grand_total))
    except (TypeError, ValueError):
        return
    if grand_total < 75:
        return
    email = (metadata.get("guest_email") or "").strip()
    if not email and metadata.get("user_id"):
        try:
            user = CustomUser.objects.get(pk=metadata.get("user_id"))
            email = (user.email or "").strip()
        except (CustomUser.DoesNotExist, ValueError, TypeError):
            pass
    if not email:
        return
    normalized_email = email.lower().strip()
    with transaction.atomic():
        if FirstPurchaseGiftCardSent.objects.filter(customer_email=normalized_email).exists():
            return
        if grand_total >= 200:
            amount = Decimal("20")
        elif grand_total >= 100:
            amount = Decimal("15")
        elif grand_total >= 75:
            amount = Decimal("10")
        else:
            return
        recipient_name = (metadata.get("guest_full_name") or "").strip() or "Valued Customer"
        gc = GiftCard.objects.create(
            initial_amount=amount,
            current_balance=amount,
            recipient_email=normalized_email,
            recipient_name=recipient_name,
            sender_name="ClassEasily",
            message="Thanks for your first class! Use this on your next booking.",
            stripe_payment_intent_id=None,
        )
        GiftCardTransaction.objects.create(
            gift_card=gc,
            amount=amount,
            balance_after=amount,
            transaction_type="initial_load",
        )
        try:
            FirstPurchaseGiftCardSent.objects.create(
                customer_email=normalized_email,
                gift_card=gc,
                order_total=grand_total,
                payment_intent_id=payment_intent_id or "",
            )
        except DjangoIntegrityError:
            return
        send_gift_card_email(gc)
        logger.info(
            "First-purchase gift card sent: %s amount=%s email=%s",
            gc.code, amount, normalized_email,
        )
    return


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
                _revalidate_for_booking(booking_record)
        except (Payment.DoesNotExist, Booking.DoesNotExist):
            logger.error(
                f"FAILURE_MARKER: Could not find pending booking/payment for PI {payment_intent_id} to mark as failed."
            )
        except Exception as e:
            logger.error(
                f"FAILURE_MARKER: An unexpected error occurred while marking PI {payment_intent_id} as failed: {str(e)}"
            )

    def _on_webhook_refund_failed(self, payment_intent_id, reason_message=""):
        """
        When Stripe refund failed after a booking could not be fulfilled: persist state
        and trigger alert so support can manually refund and/or notify the guest.
        """
        try:
            payment = Payment.objects.filter(
                stripe_payment_intent_id=payment_intent_id
            ).first()
            if payment:
                suffix = " [Stripe refund failed - manual refund required]"
                payment.failure_message = (payment.failure_message or "") + suffix
                payment.save(update_fields=["failure_message"])
        except Exception as e:
            logger.warning(
                "Could not update Payment failure_message for PI %s: %s",
                payment_intent_id, e,
            )
        logger.critical(
            "REFUND_FAILED: PaymentIntent %s - Stripe refund could not be completed. Reason: %s. Manual refund required.",
            payment_intent_id,
            reason_message,
        )

    def handle_gift_card_creation(self, payment_intent):
        """Creates the Gift Card after successful payment (Step E)"""
        meta = payment_intent.metadata
        amount = Decimal(payment_intent.amount) / 100

        # Idempotency check
        if GiftCard.objects.filter(stripe_payment_intent_id=payment_intent.id).exists():
            return

        with transaction.atomic():
            # Parse scheduled status
            is_scheduled_str = meta.get("is_scheduled", "False")
            is_scheduled = is_scheduled_str == "True"
            scheduled_date = (
                meta.get("scheduled_date") if meta.get("scheduled_date") else None
            )
            send_to_self = meta.get("send_to_self", "").lower() in ("true", "1", "yes")

            # Create Gift Card
            gc = GiftCard.objects.create(
                initial_amount=amount,
                current_balance=amount,
                recipient_email=meta.get("recipient_email"),
                recipient_name=meta.get("recipient_name"),
                sender_name=meta.get("sender_name"),
                message=meta.get("message", ""),
                design_url=meta.get("design_url"),
                is_scheduled=is_scheduled,
                scheduled_date=scheduled_date,
                send_to_self=send_to_self,
                stripe_payment_intent_id=payment_intent.id,
            )

            # Initial Load Transaction
            GiftCardTransaction.objects.create(
                gift_card=gc,
                amount=amount,
                balance_after=amount,
                transaction_type="initial_load",
            )

            logger.info(
                f"Created Gift Card {gc.code} for ${amount} via PI {payment_intent.id}"
            )

            if not is_scheduled:
                send_gift_card_email(gc)
                logger.info(f"Instant delivery email queued for Gift Card {gc.code}")
            else:
                logger.info(
                    f"Gift Card {gc.code} scheduled for {scheduled_date}. Email deferred."
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
            logger.warning("[%s] Webhook signature verification failed: %s", webhook_id, e)
            return Response(status=status.HTTP_400_BAD_REQUEST)

        logger.info(
            "[%s] Booking webhook received: event_type=%s event_id=%s",
            webhook_id,
            getattr(event, "type", "unknown"),
            getattr(event, "id", ""),
        )

        if event.type == "payment_intent.succeeded":
            payment_intent = event.data.object
            logger.info(
                f"[{webhook_id}] Processing payment_intent.succeeded for PI: {payment_intent.id}"
            )

            # 1. CHECK FOR GIFT CARD PURCHASE
            if payment_intent.metadata.get("type") == "gift_card_purchase":
                logger.info("[%s] Routing to gift card creation for PI %s", webhook_id, payment_intent.id)
                try:
                    self.handle_gift_card_creation(payment_intent)
                    logger.info("[%s] Gift card created successfully for PI %s", webhook_id, payment_intent.id)
                    return Response(status=status.HTTP_200_OK)
                except Exception as e:
                    logger.error(
                        f"[{webhook_id}] Error creating Gift Card: {e}", exc_info=True
                    )
                    return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # 2. SKIP INVOICE PAYMENTS (e.g. widget subscription) — not bookings; no CAPI
            invoice_id = getattr(payment_intent, "invoice", None) or (
                payment_intent.get("invoice") if isinstance(payment_intent, dict) else None
            )
            if invoice_id:
                logger.info(
                    f"[{webhook_id}] Skipping booking flow for PI {payment_intent.id} (invoice payment)."
                )
                return Response(status=status.HTTP_200_OK)

            # 3. STANDARD BOOKING FLOW
            logger.info("[%s] Starting standard booking flow for PI %s", webhook_id, payment_intent.id)
            try:
                response_data = self.handle_successful_payment(
                    payment_intent, webhook_id
                )
                logger.info("[%s] Booking flow completed for PI %s response=%s", webhook_id, payment_intent.id, response_data)
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
                refund_ok = self._attempt_stripe_refund(
                    payment_intent.id, f"Booking validation failed: {error_msg}"
                )
                if not refund_ok:
                    self._on_webhook_refund_failed(
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
                refund_ok = self._attempt_stripe_refund(
                    payment_intent.id, f"Unexpected server error: {e}"
                )
                if not refund_ok:
                    self._on_webhook_refund_failed(
                        payment_intent.id, f"Unexpected server error: {str(e)}"
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
            logger.info(
                "[%s] payment_intent.payment_failed PI=%s message=%s",
                webhook_id,
                payment_intent.id,
                failure_message,
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
                            "[%s] Set booking %s to cancelled due to failed payment (PI %s).",
                            webhook_id,
                            booking_to_fail.id,
                            payment_intent.id,
                        )
                        _revalidate_for_booking(booking_to_fail)

            except Payment.DoesNotExist:
                logger.warning(
                    "[%s] payment_failed: no pending payment for PI %s.",
                    webhook_id,
                    payment_intent.id,
                )
            except Exception as e:
                logger.error(
                    "[%s] Error processing payment_failed webhook for PI %s: %s",
                    webhook_id,
                    payment_intent.id,
                    str(e),
                    exc_info=True,
                )

        elif event.type == "invoice.payment_failed":
            # Widget subscription: notify business owner once per invoice (grace period to update card).
            logger.info("[%s] invoice.payment_failed received", webhook_id)
            invoice = event.data.object
            inv_id = getattr(invoice, "id", None) or (invoice.get("id") if isinstance(invoice, dict) else None)
            sub_id = getattr(invoice, "subscription", None) or (
                invoice.get("subscription") if isinstance(invoice, dict) else None
            )
            if sub_id and inv_id:
                cache_key = f"widget_sub_payment_failed_email:{inv_id}"
                if cache.get(cache_key):
                    logger.info("[%s] invoice.payment_failed: already sent email for invoice %s", webhook_id, inv_id)
                else:
                    try:
                        stripe_sub = stripe.Subscription.retrieve(sub_id)
                        meta = getattr(stripe_sub, "metadata", None) or stripe_sub.get("metadata") or {}
                        if meta.get("business_id") and not meta.get("addon_type"):
                            business_id = meta.get("business_id")
                            try:
                                business = BusinessInfo.objects.get(businessId=int(business_id))
                            except (BusinessInfo.DoesNotExist, ValueError):
                                logger.warning(
                                    "[%s] invoice.payment_failed: business %s not found for subscription %s",
                                    webhook_id, business_id, sub_id,
                                )
                            else:
                                owner = getattr(business, "owner", None)
                                if not owner or not owner.email:
                                    logger.warning(
                                        "[%s] invoice.payment_failed: no owner email for business %s",
                                        webhook_id, business.businessId,
                                    )
                                else:
                                    plan_id = (meta.get("plan_id") or "growth").strip().lower()
                                    if plan_id not in ("basic", "growth", "advanced"):
                                        plan_id = "growth"
                                    plan_display = plan_id.capitalize()
                                    settings_billing_url = f"{settings.FRONTEND_BASE_URL or ''}/business/dashboard?tab=settings"
                                    grace_days = getattr(settings, "WIDGET_SUBSCRIPTION_PAYMENT_FAILED_GRACE_DAYS", 7)
                                    send_widget_subscription_payment_failed_email(
                                        business_user=owner,
                                        business_name=getattr(business, "businessName", None) or f"Business {business_id}",
                                        plan_name=plan_display,
                                        settings_billing_url=settings_billing_url,
                                        grace_days=grace_days,
                                    )
                                    cache.set(cache_key, True, timeout=7 * 24 * 3600)  # 7 days
                                    logger.info(
                                        "[%s] Sent widget subscription payment failed email to %s for invoice %s",
                                        webhook_id, owner.email, inv_id,
                                    )
                    except (stripe.StripeError, Exception) as e:
                        logger.warning(
                            "[%s] invoice.payment_failed handling failed: %s", webhook_id, e, exc_info=True,
                        )
            return Response(status=status.HTTP_200_OK)

        elif event.type == "invoice.paid":
            # When a widget subscription invoice is paid (e.g. plan switch proration), sync plan_id.
            # Derive plan_id from the subscription's current price (source of truth), not metadata,
            # because invoice.paid can fire before our second modify(metadata) is applied (race).
            logger.info("[%s] invoice.paid received", webhook_id)
            invoice = event.data.object
            sub_id = getattr(invoice, "subscription", None) or (
                invoice.get("subscription") if isinstance(invoice, dict) else None
            )
            if sub_id:
                try:
                    stripe_sub = stripe.Subscription.retrieve(sub_id)
                    meta = getattr(stripe_sub, "metadata", None) or stripe_sub.get("metadata") or {}
                    if meta.get("business_id") and not meta.get("addon_type"):
                        # Prefer metadata.plan_id (set when user requested plan switch) so we don't sync the old
                        # price when invoice.paid fires before Stripe has updated the subscription's items.
                        price_to_plan = {
                            getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC", None): "basic",
                            getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH", None): "growth",
                            getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED", None): "advanced",
                        }
                        stripe_price_id = None
                        items = (stripe_sub.get("items") or {}).get("data") or []
                        if items and items[0].get("price"):
                            price_obj = items[0]["price"]
                            stripe_price_id = price_obj.get("id") if isinstance(price_obj, dict) else getattr(price_obj, "id", None)
                        plan_id_from_meta = (meta.get("plan_id") or "").strip().lower()
                        if plan_id_from_meta in ("basic", "growth", "advanced"):
                            plan_id = plan_id_from_meta
                        else:
                            plan_id = (price_to_plan.get(stripe_price_id) or (meta.get("plan_id") or "growth")).strip().lower()
                        if plan_id not in ("basic", "growth", "advanced"):
                            plan_id = "growth"
                        cancel_at_period_end = bool(getattr(stripe_sub, "cancel_at_period_end", None) or stripe_sub.get("cancel_at_period_end"))
                        WidgetSubscription.objects.filter(
                            stripe_subscription_id=stripe_sub.id
                        ).update(plan_id=plan_id, cancel_at_period_end=cancel_at_period_end)
                        logger.info(
                            f"[{webhook_id}] Widget subscription {stripe_sub.id} plan_id synced to {plan_id} after invoice.paid (price_id={stripe_price_id})."
                        )
                except (stripe.StripeError, Exception) as e:
                    logger.warning(
                        f"[{webhook_id}] invoice.paid subscription sync failed: %s", e
                    )
            return Response(status=status.HTTP_200_OK)

        elif event.type in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            logger.info("[%s] Subscription event: %s", webhook_id, event.type)
            subscription = event.data.object
            metadata = getattr(subscription, "metadata", None) or subscription.get("metadata") or {}
            business_id = metadata.get("business_id")
            if not business_id:
                logger.warning(
                    f"[{webhook_id}] Subscription {subscription.id} has no business_id in metadata; skipping."
                )
                return Response(status=status.HTTP_200_OK)
            try:
                business = BusinessInfo.objects.get(businessId=int(business_id))
            except (BusinessInfo.DoesNotExist, ValueError):
                logger.warning(
                    f"[{webhook_id}] Business {business_id} not found for subscription {subscription.id}."
                )
                return Response(status=status.HTTP_200_OK)

            addon_type = metadata.get("addon_type")
            if addon_type == ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING:
                from datetime import datetime
                import pytz
                if event.type == "customer.subscription.deleted":
                    BusinessAddonSubscription.objects.filter(
                        stripe_subscription_id=subscription.id
                    ).update(status="canceled")
                    business.marketplace_email_branding_enabled = False
                    business.save(update_fields=["marketplace_email_branding_enabled"])
                    logger.info(
                        f"[{webhook_id}] Addon {addon_type} subscription {subscription.id} canceled; disabled for business {business.businessId}."
                    )
                else:
                    period_end = subscription.current_period_end
                    current_period_end = (
                        datetime.fromtimestamp(period_end, tz=pytz.UTC)
                        if period_end
                        else None
                    )
                    stripe_price_id = None
                    if subscription.get("items") and subscription["items"].get("data"):
                        stripe_price_id = subscription["items"]["data"][0].get("price", {}).get("id")
                    cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
                    BusinessAddonSubscription.objects.update_or_create(
                        stripe_subscription_id=subscription.id,
                        defaults={
                            "business": business,
                            "addon_type": addon_type,
                            "stripe_customer_id": subscription.get("customer") or "",
                            "stripe_price_id": stripe_price_id,
                            "status": subscription.status,
                            "current_period_end": current_period_end,
                            "cancel_at_period_end": cancel_at_period_end,
                        },
                    )
                    business.marketplace_email_branding_enabled = subscription.status in ("active", "trialing")
                    business.save(update_fields=["marketplace_email_branding_enabled"])
                    logger.info(
                        f"[{webhook_id}] Synced addon {addon_type} subscription {subscription.id} for business {business.businessId} (status={subscription.status})."
                    )
                return Response(status=status.HTTP_200_OK)

            if event.type == "customer.subscription.deleted":
                WidgetSubscription.objects.filter(
                    stripe_subscription_id=subscription.id
                ).update(status="canceled")
                logger.info(
                    f"[{webhook_id}] Marked widget subscription {subscription.id} as canceled."
                )
            else:
                from datetime import datetime
                import pytz
                period_end = subscription.current_period_end
                current_period_end = (
                    datetime.fromtimestamp(period_end, tz=pytz.UTC)
                    if period_end
                    else None
                )
                stripe_price_id = None
                if subscription.get("items") and subscription["items"].get("data"):
                    stripe_price_id = subscription["items"]["data"][0].get("price", {}).get("id")
                plan_id_from_metadata = (metadata.get("plan_id") or "growth").strip().lower()
                if plan_id_from_metadata not in ("basic", "growth", "advanced"):
                    plan_id_from_metadata = "growth"
                # Only apply plan_id from metadata when latest invoice is paid (avoid showing new plan before payment on switch).
                existing = WidgetSubscription.objects.filter(
                    stripe_subscription_id=subscription.id
                ).first()
                try:
                    sub_expanded = stripe.Subscription.retrieve(
                        subscription.id, expand=["latest_invoice"]
                    )
                    latest_inv = sub_expanded.get("latest_invoice")
                    inv_status = (
                        getattr(latest_inv, "status", None)
                        if latest_inv and not isinstance(latest_inv, str)
                        else (latest_inv.get("status") if isinstance(latest_inv, dict) else None)
                    )
                    if inv_status in ("open", "draft") and existing:
                        plan_id = existing.plan_id
                    else:
                        plan_id = plan_id_from_metadata
                except stripe.StripeError:
                    plan_id = plan_id_from_metadata
                cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
                defaults = {
                    "business": business,
                    "stripe_customer_id": subscription.get("customer") or "",
                    "stripe_price_id": stripe_price_id,
                    "status": subscription.status,
                    "current_period_end": current_period_end,
                    "plan_id": plan_id,
                    "cancel_at_period_end": cancel_at_period_end,
                }
                existing_by_stripe_id = WidgetSubscription.objects.filter(
                    stripe_subscription_id=subscription.id
                ).first()
                if existing_by_stripe_id:
                    for k, v in defaults.items():
                        setattr(existing_by_stripe_id, k, v)
                    existing_by_stripe_id.save()
                    sub = existing_by_stripe_id
                else:
                    # Attach to existing DB-only row if any (avoid duplicate when webhook runs before backend save)
                    db_only = WidgetSubscription.objects.filter(
                        business=business, stripe_subscription_id__isnull=True
                    ).exclude(stripe_subscription_id="").first()
                    if db_only:
                        db_only.stripe_subscription_id = subscription.id
                        for k, v in defaults.items():
                            setattr(db_only, k, v)
                        db_only.save()
                        sub = db_only
                    else:
                        sub, _ = WidgetSubscription.objects.update_or_create(
                            stripe_subscription_id=subscription.id,
                            defaults=defaults,
                        )
                if not business.stripe_customer_id and subscription.get("customer"):
                    business.stripe_customer_id = subscription["customer"]
                    business.save(update_fields=["stripe_customer_id"])
                logger.info(
                    f"[{webhook_id}] Synced widget subscription {subscription.id} for business {business.businessId} (status={subscription.status})."
                )
            return Response(status=status.HTTP_200_OK)

        return Response(status=status.HTTP_200_OK)

    def handle_course_payment_success(self, payment_intent, webhook_id):
        """
        Handle successful payment for a course enrollment.
        Updates CourseEnrollment and all session Bookings with 1/N payout allocation.
        Redeems discounts if applicable.
        """
        booking_group_id = payment_intent.metadata.get("booking_group_id")
        logger.info(
            "[%s] handle_course_payment_success PI=%s booking_group_id=%s",
            webhook_id,
            payment_intent.id,
            booking_group_id,
        )
        try:
            with transaction.atomic():
                # Get booking group ID from metadata
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
                # Business payout is based on subtotal_for_payout (before global discount) so business never loses
                subtotal_for_payout = Decimal(
                    metadata.get("subtotal_for_payout")
                    or metadata.get("subtotal_after_discount", "0.00")
                )
                total_tax = Decimal(metadata.get("tax_amount", "0.00"))
                grand_total = Decimal(payment_intent.amount_received) / 100

                # 2. Calculate Business Net Revenue (Total Net Payout) from subtotal_for_payout
                business = enrollment.schedule.option.classId.businessId
                if metadata.get("booking_source") == "widget":
                    plan_id = (metadata.get("plan_id") or "basic").lower()
                    fee_percentage = {
                        "basic": Decimal("4.00"),
                        "growth": Decimal("3.00"),
                        "advanced": Decimal("2.00"),
                    }.get(plan_id, Decimal("4.00"))
                else:
                    fee_percentage = (
                        business.partner_tier.fee_percentage
                        if business.partner_tier
                        else PartnerTier.objects.get(is_default=True).fee_percentage
                    )

                service_fee_rate = fee_percentage / Decimal("100.0")
                platform_fee_amount = (
                    subtotal_for_payout * service_fee_rate
                ).quantize(Decimal("0.01"))
                platform_fee_tax = (platform_fee_amount * HST_RATE).quantize(
                    Decimal("0.01")
                )

                # This is the total bucket of money the business is owed for the whole course
                business_payout_tax = total_tax - platform_fee_tax
                business_net_revenue = subtotal_for_payout - platform_fee_amount
                total_net_payout_to_business = (
                    business_net_revenue + business_payout_tax
                )

                # 3. Calculate Share Per Booking
                booking_count = len(bookings)
                if booking_count > 0:
                    share_per_booking = (
                        total_net_payout_to_business / booking_count
                    ).quantize(Decimal("0.01"))

                    # Handle rounding remainders (e.g. 100 / 3 = 33.33, 33.33, 33.33 -> Remainder 0.01)
                    total_allocated = share_per_booking * booking_count
                    remainder = total_net_payout_to_business - total_allocated
                else:
                    share_per_booking = Decimal("0.00")
                    remainder = Decimal("0.00")

                # 4. Update Bookings
                generated_refs = set()

                # Calculate session price for display (customer facing amount)
                session_price = (enrollment.total_amount_paid / booking_count).quantize(
                    Decimal("0.01")
                )

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
                        discount = Discount.objects.select_for_update().get(
                            pk=applied_discount_id
                        )
                        discount.redeem()  # Increments usage_count atomically

                        # Distribute the applied discount amount across bookings for record-keeping
                        if booking_count > 0:
                            share_discount = (
                                total_discount_amount / booking_count
                            ).quantize(Decimal("0.01"))
                            total_allocated_discount = share_discount * booking_count
                            remainder_discount = (
                                total_discount_amount - total_allocated_discount
                            )

                            applied_discounts = []
                            for index, booking in enumerate(bookings):
                                amount = share_discount
                                if index == 0:
                                    amount += remainder_discount

                                applied_discounts.append(
                                    AppliedDiscount(
                                        booking=booking,
                                        discount=discount,
                                        amount_saved=amount,
                                    )
                                )

                            AppliedDiscount.objects.bulk_create(applied_discounts)
                            logger.info(
                                f"[{webhook_id}] Redeemed discount {discount.code} for course (Group: {booking_group_id})."
                            )

                    except Discount.DoesNotExist:
                        logger.warning(
                            f"[{webhook_id}] Discount ID {applied_discount_id} found in metadata but not in DB."
                        )

                # --- Handle Global Discount Redemption (Course) ---
                global_discount_id = metadata.get("global_discount_id")
                global_discount_amount = Decimal(
                    metadata.get("global_discount_amount", "0.00")
                )
                if global_discount_id and global_discount_amount > 0:
                    try:
                        g_discount = GlobalDiscount.objects.select_for_update().get(
                            pk=global_discount_id
                        )
                        g_discount.redeem()
                        if booking_count > 0:
                            share_g = (
                                global_discount_amount / booking_count
                            ).quantize(Decimal("0.01"))
                            total_alloc_g = share_g * booking_count
                            remainder_g = global_discount_amount - total_alloc_g
                            for index, b in enumerate(bookings):
                                amt = share_g + (
                                    remainder_g if index == 0 else Decimal("0")
                                )
                                AppliedGlobalDiscount.objects.create(
                                    booking=b,
                                    global_discount=g_discount,
                                    amount_saved=amt,
                                )
                        logger.info(
                            f"[{webhook_id}] Redeemed global discount {g_discount.name} for course (Group: {booking_group_id})."
                        )
                    except GlobalDiscount.DoesNotExist:
                        logger.warning(
                            f"[{webhook_id}] Global discount ID {global_discount_id} not found in DB."
                        )

                # --- Handle Gift Card Deduction (Step D) ---
                gc_code = metadata.get("gift_card_code")
                gc_amount_str = metadata.get("gift_card_amount_to_deduct")

                if gc_code and gc_amount_str:
                    try:
                        gc_amount = Decimal(gc_amount_str)
                        if gc_amount > 0:
                            gc = GiftCard.objects.select_for_update().get(code=gc_code)
                            if gc.current_balance >= gc_amount:
                                gc.current_balance -= gc_amount
                                gc.save()

                                GiftCardTransaction.objects.create(
                                    gift_card=gc,
                                    booking=bookings[
                                        0
                                    ],  # Associate with first booking of the course
                                    amount=-gc_amount,
                                    balance_after=gc.current_balance,
                                    transaction_type="redemption",
                                )
                                logger.info(
                                    f"[{webhook_id}] Deducted ${gc_amount} from GC {gc_code} for Course."
                                )
                            else:
                                logger.critical(
                                    f"[{webhook_id}] Insufficient funds in GC {gc_code} for committed transaction. Manual review required."
                                )
                    except Exception as e:
                        logger.error(
                            f"[{webhook_id}] Failed to process GC deduction: {e}",
                            exc_info=True,
                        )

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
                payment_record.net_payout_amount = (
                    total_net_payout_to_business  # Total for the whole course
                )
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
                        payment_record.card_brand = (
                            charge_details.payment_method_details.card.brand
                        )
                        payment_record.card_last4 = (
                            charge_details.payment_method_details.card.last4
                        )

                payment_record.save()

                # --- Emails & Notifications (Existing logic) ---
                first_booking = bookings[0]
                recipient_user = first_booking.user
                recipient_contact = first_booking.contact

                if recipient_user:
                    send_booking_confirmation_email(recipient_user, first_booking)
                elif recipient_contact:
                    send_booking_confirmation_email(recipient_contact, first_booking)

                if business_sms_enabled(business):
                    booker = recipient_user or recipient_contact
                    phone = getattr(booker, "phone_number", None) if booker else None
                    if not phone and first_booking.metadata:
                        phone = first_booking.metadata.get("guest_phone") or ""
                    normalized = normalize_phone_for_sns(phone or "")
                    if normalized and first_booking.schedule_instance:
                        class_title = getattr(first_booking.schedule_instance.schedule.option.classId, "title", "Class")
                        date_str = first_booking.schedule_instance.date.strftime("%b %d")
                        t = first_booking.schedule_instance.time
                        time_str = t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else str(t)
                        business_name = getattr(business, "businessName", "") or "ClassEasily"
                        sms_msg = f"You're in! {class_title} is on {date_str} at {time_str}.\n\nAdd it to your calendar — we'll send a reminder the day before.\n\n— {business_name}"
                        try:
                            send_sms_task.delay(normalized, sms_msg)
                        except Exception as sms_e:
                            logger.warning("Booking confirmation SMS failed: %s", sms_e)

                if business.newBookingNotification:
                    recipients = {business.owner}
                    staff_to_notify = BusinessStaff.objects.filter(
                        business=business,
                        status="accepted",
                        role__permissions__codename="receive_booking_notifications",
                    ).select_related("user")

                    for staff in staff_to_notify:
                        if staff.user:
                            recipients.add(staff.user)

                    for recipient in recipients:
                        if recipient and recipient.email:
                            send_business_new_booking_email(recipient, first_booking)

                    if business_sms_enabled(business) and first_booking.schedule_instance:
                        class_title = getattr(first_booking.schedule_instance.schedule.option.classId, "title", "Class")
                        date_str = first_booking.schedule_instance.date.strftime("%b %d")
                        t = first_booking.schedule_instance.time
                        time_str = t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else str(t)
                        booker = recipient_user or recipient_contact
                        if booker and hasattr(booker, "first_name") and hasattr(booker, "last_name"):
                            booker_name = f"{getattr(booker, 'first_name', '')} {getattr(booker, 'last_name', '')}".strip() or getattr(booker, "email", "A customer")
                        elif booker:
                            booker_name = getattr(booker, "email", "A customer") or "A customer"
                        else:
                            booker_name = "A customer"
                        sms_msg = f"New booking: {class_title} on {date_str} at {time_str}.\n\nBooked by {booker_name}. Check your dashboard for details.\n\n— ClassEasily"
                        for r in recipients:
                            if r and normalize_phone_for_sns(getattr(r, "phone_number", None) or ""):
                                try:
                                    send_sms_task.delay(normalize_phone_for_sns(r.phone_number), sms_msg)
                                except Exception as sms_e:
                                    logger.warning("New booking SMS failed: %s", sms_e)

                    send_super_admin_booking_created_email(first_booking)

                logger.info(
                    f"[{webhook_id}] 1/N Payout processed. Total Net: {total_net_payout_to_business}, Per Booking: {share_per_booking}"
                )
                # Meta CAPI: server-side Purchase with deduplication (event_id = first booking id)
                # Use fbc/fbp from PaymentIntent metadata (stored at create-payment-intent) for paid conversions
                try:
                    send_purchase_event_for_booking(
                        first_booking,
                        value=float(enrollment.total_amount_paid),
                        currency="CAD",
                        num_items=first_booking.participants or 1,
                        meta_fbc=payment_intent.metadata.get("meta_fbc") or None,
                        meta_fbp=payment_intent.metadata.get("meta_fbp") or None,
                    )
                except Exception as capi_err:
                    logger.warning(
                        "[%s] Meta CAPI send failed (non-fatal): %s",
                        webhook_id,
                        capi_err,
                    )
                _revalidate_for_booking(first_booking)
                try:
                    grand_total = Decimal(payment_intent.amount_received) / 100
                    try_send_first_purchase_gift_card(
                        dict(payment_intent.metadata),
                        grand_total,
                        payment_intent_id=payment_intent.id,
                    )
                except Exception as fp_err:
                    logger.warning(
                        "[%s] First-purchase gift card failed (non-fatal): %s",
                        webhook_id, fp_err, exc_info=True,
                    )
                return {"message": "Course payment processed successfully"}

        except Exception as e:
            logger.error(
                f"[{webhook_id}] Error processing course payment success: {e}",
                exc_info=True,
            )
            raise

    def _create_single_booking_from_metadata(self, payment_intent, webhook_id):
        """
        Instant flow (no hold): create confirmed booking + payment from PaymentIntent metadata.
        Raises DRFValidationError if slot no longer has capacity (caller will refund).
        """
        metadata = payment_intent.metadata
        logger.info(
            "[%s] WEBHOOK _create_single_booking_from_metadata PI=%s METADATA READ: guest_email=%s guest_full_name=%s guest_phone=%s booking_source=%s is_guest=%s user_id=%s",
            webhook_id,
            payment_intent.id,
            metadata.get("guest_email"),
            metadata.get("guest_full_name"),
            metadata.get("guest_phone"),
            metadata.get("booking_source"),
            metadata.get("is_guest"),
            metadata.get("user_id"),
        )
        instance_id = metadata.get("schedule_instance_id")
        logger.info("[%s] _create_single_booking_from_metadata PI=%s schedule_instance_id=%s", webhook_id, payment_intent.id, instance_id)

        if not instance_id:
            logger.error("[%s] Missing schedule_instance_id in metadata", webhook_id)
            raise DRFValidationError(
                "Missing schedule_instance_id in metadata. Initiating refund."
            )
        try:
            instance = ScheduleInstance.objects.select_related(
                "schedule__option__classId__businessId"
            ).get(pk=int(instance_id))
            logger.info("[%s] Resolved ScheduleInstance id=%s date=%s", webhook_id, instance.id, getattr(instance, "date", None))
        except (ValueError, ScheduleInstance.DoesNotExist):
            logger.warning("[%s] Invalid or missing schedule instance id=%s", webhook_id, instance_id)
            raise DRFValidationError(
                "Invalid or missing schedule instance. Initiating refund."
            )

        participants = int(metadata.get("participants", 1))
        if not instance.can_accommodate(participants):
            logger.warning("[%s] Slot no longer has capacity for %s participants", webhook_id, participants)
            raise DRFValidationError(
                f"Session on {instance.date.strftime('%b %d')} is now full. Initiating refund."
            )

        option = instance.schedule.option
        business = option.classId.businessId
        is_guest = metadata.get("is_guest") == "True"
        # Widget (and similar flows) set guest_* via update-payment-intent but may not set is_guest/guest_contact_id
        guest_email = metadata.get("guest_email") or ""
        guest_full_name = (metadata.get("guest_full_name") or "").strip()
        guest_phone = metadata.get("guest_phone") or ""
        is_widget_guest = metadata.get("booking_source") == "widget" or (
            not metadata.get("user_id") and (guest_email or guest_full_name)
        )
        if is_widget_guest:
            is_guest = True

        try:
            participant_details = json.loads(
                metadata.get("participant_details_json", "[]")
            )
        except (TypeError, ValueError):
            participant_details = []
        if not isinstance(participant_details, list):
            participant_details = []
        # Pad or trim to participants length
        participant_details = [
            (participant_details[i] if i < len(participant_details) else {})
            for i in range(participants)
        ]
        participant_details = [
            {"name": (p.get("name") or "Guest") if isinstance(p, dict) else "Guest"}
            for p in participant_details
        ]

        user = None
        contact = None
        if is_guest:
            if metadata.get("guest_contact_id"):
                try:
                    # Prefer contact by (business, email) to avoid unique constraint when
                    # intent was created with placeholder email then updated to real email
                    # that already exists for this business.
                    if guest_email:
                        contact = Contact.objects.filter(
                            business=business,
                            email__iexact=guest_email,
                        ).first()
                        if contact:
                            first = (guest_full_name or "Guest").split(" ", 1)[0]
                            last = (guest_full_name or "Guest").split(" ", 1)[-1] if len((guest_full_name or "Guest").split(" ", 1)) > 1 else ""
                            Contact.objects.filter(pk=contact.pk).update(
                                first_name=first,
                                last_name=last,
                                phone_number=guest_phone or "",
                                updated_at=timezone.now(),
                            )
                        else:
                            contact = Contact.objects.get(pk=metadata.get("guest_contact_id"))
                            if guest_email:
                                contact.email = guest_email
                            if guest_full_name:
                                parts = guest_full_name.split(" ", 1)
                                contact.first_name = parts[0]
                                contact.last_name = parts[1] if len(parts) > 1 else ""
                            if guest_phone:
                                contact.phone_number = guest_phone
                            try:
                                contact.save()
                            except DjangoIntegrityError:
                                contact = Contact.objects.get(
                                    business=business,
                                    email__iexact=guest_email,
                                )
                                first = (guest_full_name or "Guest").split(" ", 1)[0]
                                last = (guest_full_name or "Guest").split(" ", 1)[-1] if len((guest_full_name or "Guest").split(" ", 1)) > 1 else ""
                                Contact.objects.filter(pk=contact.pk).update(
                                    first_name=first,
                                    last_name=last,
                                    phone_number=guest_phone or "",
                                    updated_at=timezone.now(),
                                )
                    else:
                        contact = Contact.objects.get(pk=metadata.get("guest_contact_id"))
                        if guest_full_name:
                            parts = guest_full_name.split(" ", 1)
                            contact.first_name = parts[0]
                            contact.last_name = parts[1] if len(parts) > 1 else ""
                        if guest_phone:
                            contact.phone_number = guest_phone
                        Contact.objects.filter(pk=contact.pk).update(
                            first_name=contact.first_name,
                            last_name=contact.last_name,
                            phone_number=contact.phone_number,
                            updated_at=timezone.now(),
                        )
                except (ValueError, TypeError, Contact.DoesNotExist):
                    raise DRFValidationError(
                        "Guest contact missing. Initiating refund."
                    )
            elif guest_email or guest_full_name:
                # Widget/single-session: no guest_contact_id; get existing contact or create (never full save to avoid unique constraint)
                if not guest_email:
                    raise DRFValidationError(
                        "Guest email missing. Initiating refund."
                    )
                first = (guest_full_name or "Guest").split(" ", 1)[0]
                last = (guest_full_name or "Guest").split(" ", 1)[-1] if len((guest_full_name or "Guest").split(" ", 1)) > 1 else ""
                contact = Contact.objects.filter(
                    business=business,
                    email__iexact=guest_email,
                ).first()
                if contact:
                    contact.first_name = first
                    contact.last_name = last
                    contact.phone_number = guest_phone or ""
                    Contact.objects.filter(pk=contact.pk).update(
                        first_name=contact.first_name,
                        last_name=contact.last_name,
                        phone_number=contact.phone_number,
                        updated_at=timezone.now(),
                    )
                else:
                    try:
                        contact = Contact.objects.create(
                            business=business,
                            email=guest_email,
                            first_name=first,
                            last_name=last,
                            phone_number=guest_phone or "",
                            source="widget_booking",
                        )
                    except DjangoIntegrityError:
                        contact = Contact.objects.get(
                            business=business,
                            email__iexact=guest_email,
                        )
                        Contact.objects.filter(pk=contact.pk).update(
                            first_name=first,
                            last_name=last,
                            phone_number=guest_phone or "",
                            updated_at=timezone.now(),
                        )
            else:
                raise DRFValidationError(
                    "Guest contact missing. Initiating refund."
                )
            if contact and is_placeholder_guest_contact(contact):
                raise DRFValidationError(
                    "Guest contact details are invalid. Initiating refund."
                )
        else:
            try:
                user = CustomUser.objects.get(
                    userId=metadata.get("user_id")
                )
            except (ValueError, TypeError, CustomUser.DoesNotExist):
                raise DRFValidationError(
                    "User missing. Initiating refund."
                )

        notes = metadata.get("notes", "") or ""
        grand_total = Decimal(payment_intent.amount_received) / 100
        total_tax = Decimal(metadata.get("tax_amount", "0.00"))
        subtotal_for_payout = Decimal(
            metadata.get("subtotal_for_payout")
            or metadata.get("subtotal_after_discount", "0.00")
        )

        with transaction.atomic():
            booking = Booking.objects.create(
                schedule_instance=instance,
                user=user,
                contact=contact,
                participants=participants,
                participant_details=participant_details,
                notes=notes,
                amount_paid=grand_total,
                status="confirmed",
                payment_status="paid",
                enrollment_type="Single Session",
                cancellation_policy=option.cancellationPolicy,
                cancellation_refund_percentage=option.cancellationRefundPercentage,
                cancellation_custom_hours=option.cancellationCustomHours,
            )
            booking.user_facing_reference = booking._generate_user_facing_reference()
            booking.save(update_fields=["user_facing_reference"])
            logger.info("[%s] Created booking id=%s user_facing_reference=%s", webhook_id, booking.id, booking.user_facing_reference)

            if contact and not user:
                booking.cancellation_token = uuid.uuid4()
                booking.save(update_fields=["cancellation_token"])

            fee_percentage = (
                business.partner_tier.fee_percentage
                if business.partner_tier
                else PartnerTier.objects.get(is_default=True).fee_percentage
            )
            service_fee_rate = fee_percentage / Decimal("100.0")
            platform_fee_amount = (
                subtotal_for_payout * service_fee_rate
            ).quantize(Decimal("0.01"))
            platform_fee_tax = (platform_fee_amount * HST_RATE).quantize(
                Decimal("0.01")
            )
            business_payout_tax = total_tax - platform_fee_tax
            business_net_revenue = subtotal_for_payout - platform_fee_amount
            net_payout_to_business = business_net_revenue + business_payout_tax
            booking.allocated_net_payout = net_payout_to_business
            booking.save(update_fields=["allocated_net_payout"])

            Payment.objects.create(
                booking=booking,
                stripe_payment_intent_id=payment_intent.id,
                stripe_charge_id=payment_intent.latest_charge,
                amount=grand_total,
                tax_amount=total_tax,
                currency="CAD",
                status="succeeded",
                platform_fee_amount=platform_fee_amount,
                platform_fee_tax=platform_fee_tax,
                net_payout_amount=net_payout_to_business,
                metadata={"original_stripe_metadata": dict(metadata)},
            )
            logger.info("[%s] Created Payment record for booking %s PI=%s", webhook_id, booking.id, payment_intent.id)

            applied_discount_id = metadata.get("applied_discount_id")
            discount_amount = Decimal(metadata.get("discount_amount", "0.00"))
            if applied_discount_id:
                try:
                    discount = Discount.objects.get(pk=applied_discount_id)
                    discount.redeem()
                    AppliedDiscount.objects.create(
                        booking=booking,
                        discount=discount,
                        amount_saved=discount_amount,
                    )
                except Discount.DoesNotExist:
                    pass

            global_discount_id = metadata.get("global_discount_id")
            global_discount_amount = Decimal(
                metadata.get("global_discount_amount", "0.00")
            )
            if global_discount_id and global_discount_amount > 0:
                try:
                    g_discount = GlobalDiscount.objects.get(pk=global_discount_id)
                    g_discount.redeem()
                    AppliedGlobalDiscount.objects.create(
                        booking=booking,
                        global_discount=g_discount,
                        amount_saved=global_discount_amount,
                    )
                except GlobalDiscount.DoesNotExist:
                    pass

            gc_code = metadata.get("gift_card_code")
            gc_amount_str = metadata.get("gift_card_amount_to_deduct")
            if gc_code and gc_amount_str:
                try:
                    gc_amount = Decimal(gc_amount_str)
                    if gc_amount > 0:
                        gc = GiftCard.objects.select_for_update().get(code=gc_code)
                        if gc.current_balance >= gc_amount:
                            gc.current_balance -= gc_amount
                            gc.save()
                            GiftCardTransaction.objects.create(
                                gift_card=gc,
                                booking=booking,
                                amount=-gc_amount,
                                balance_after=gc.current_balance,
                                transaction_type="redemption",
                            )
                        else:
                            logger.critical(
                                "Insufficient gift card balance at deduction (instant single): code=%s, required=%s, current_balance=%s. Manual review required.",
                                gc_code,
                                gc_amount,
                                gc.current_balance,
                            )
                except GiftCard.DoesNotExist:
                    logger.warning("Gift card not found for instant single booking deduction: code=%s", gc_code)
                except Exception as e:
                    logger.critical(
                        "Gift card deduction failed for instant single booking: code=%s, error=%s. Manual review required.",
                        gc_code,
                        e,
                        exc_info=True,
                    )

            if user:
                send_booking_confirmation_email(user, booking)
            elif contact:
                send_booking_confirmation_email(contact, booking)

            if business_sms_enabled(business):
                booker = user or contact
                phone = getattr(booker, "phone_number", None) if booker else (booking.metadata or {}).get("guest_phone") or ""
                normalized = normalize_phone_for_sns(phone or "")
                if normalized:
                    class_title = getattr(booking.schedule_instance.schedule.option.classId, "title", "Class")
                    date_str = booking.schedule_instance.date.strftime("%b %d") if booking.schedule_instance and booking.schedule_instance.date else ""
                    try:
                        send_sms_task.delay(normalized, f"You're booked for {class_title} on {date_str}. ClassEasily")
                    except Exception as sms_e:
                        logger.warning("Booking confirmation SMS failed: %s", sms_e)

            if business.newBookingNotification:
                recipients = {business.owner}
                for staff in BusinessStaff.objects.filter(
                    business=business,
                    status="accepted",
                    role__permissions__codename="receive_booking_notifications",
                ).select_related("user"):
                    if staff.user:
                        recipients.add(staff.user)
                for r in recipients:
                    if r and r.email:
                        send_business_new_booking_email(r, booking)
                if business_sms_enabled(business):
                    class_title = getattr(booking.schedule_instance.schedule.option.classId, "title", "Class")
                    date_str = booking.schedule_instance.date.strftime("%b %d") if booking.schedule_instance and booking.schedule_instance.date else ""
                    sms_msg = f"New booking: {class_title} on {date_str}. ClassEasily"
                    for r in recipients:
                        if r:
                            normalized = normalize_phone_for_sns(getattr(r, "phone_number", None) or "")
                            if normalized:
                                try:
                                    send_sms_task.delay(normalized, sms_msg)
                                except Exception as sms_e:
                                    logger.warning("New booking SMS failed: %s", sms_e)
                send_super_admin_booking_created_email(booking)

            try:
                send_purchase_event_for_booking(
                    booking,
                    value=float(grand_total),
                    currency="CAD",
                    num_items=booking.participants or 1,
                    meta_fbc=metadata.get("meta_fbc") or None,
                    meta_fbp=metadata.get("meta_fbp") or None,
                )
            except Exception:
                pass
            _revalidate_for_booking(booking)

        try:
            try_send_first_purchase_gift_card(
                dict(metadata),
                grand_total,
                payment_intent_id=payment_intent.id,
            )
        except Exception as fp_err:
            logger.warning(
                "[%s] First-purchase gift card failed (non-fatal): %s",
                webhook_id, fp_err, exc_info=True,
            )
        logger.info(
            f"[{webhook_id}] Created single booking from metadata: {booking.id}"
        )
        return {
            "booking_id": booking.id,
            "user_facing_reference": booking.user_facing_reference,
        }

    def handle_successful_payment(self, payment_intent, webhook_id):
        logger.info("[%s] handle_successful_payment PI=%s", webhook_id, payment_intent.id)

        # Check if we have ANY record for this Stripe ID that isn't 'pending'.
        # This catches 'refunded', 'failed', and 'succeeded' statuses safely.
        existing_payment = (
            Payment.objects.filter(stripe_payment_intent_id=payment_intent.id)
            .exclude(status="pending")
            .first()
        )

        if existing_payment:
            logger.warning(
                "[%s] IDEMPOTENCY: PI %s already processed, status=%s",
                webhook_id,
                payment_intent.id,
                existing_payment.status,
            )
            return {"message": "Already processed"}

        # 2. Check for Course vs Single Session
        # Use correct metadata key 'booking_type' as sent by CreatePaymentIntentView
        enrollment_type = payment_intent.metadata.get("booking_type")
        logger.info("[%s] booking_type from metadata: %s", webhook_id, enrollment_type)

        if enrollment_type == "Full Course":
            logger.info(
                "[%s] Routing to course handler for PI %s",
                webhook_id,
                payment_intent.id,
            )
            return self.handle_course_payment_success(payment_intent, webhook_id)

        # 3. Process Single Session Booking
        payment_record = (
            Payment.objects.filter(
                stripe_payment_intent_id=payment_intent.id, status="pending"
            )
            .first()
        )

        if not payment_record:
            # Instant flow (no hold): create booking from metadata; availability check then create
            logger.info("[%s] No pending payment: creating single booking from metadata (instant flow)", webhook_id)
            try:
                return self._create_single_booking_from_metadata(
                    payment_intent, webhook_id
                )
            except DRFValidationError:
                raise

        logger.info("[%s] Pending payment found: confirming single-session booking (with hold)", webhook_id)
        with transaction.atomic():
            # Lock the payment row by PI id (do not filter by status) so we can safely
            # distinguish "already processed" from "missing". Otherwise a concurrent
            # delivery could mark the payment succeeded between our earlier query and
            # here, we'd get no row (status filter), and we'd incorrectly trigger a refund.
            payment_record = (
                Payment.objects.select_for_update()
                .filter(stripe_payment_intent_id=payment_intent.id)
                .first()
            )

            if not payment_record:
                logger.error("[%s] Payment record missing for PI %s", webhook_id, payment_intent.id)
                raise DRFValidationError(
                    "Payment record missing. Initiating refund to prevent lost funds."
                )
            if payment_record.status != "pending":
                logger.warning(
                    "[%s] IDEMPOTENCY: PI %s already processed (status=%s). Skipping without refund.",
                    webhook_id,
                    payment_intent.id,
                    payment_record.status,
                )
                return {"message": "Already processed"}

            pending_booking = (
                Booking.objects.select_for_update()
                .filter(pk=payment_record.booking.pk, status="pending")
                .first()
            )

            if not pending_booking:
                raise DRFValidationError(
                    "Booking record missing in DB. Initiating refund."
                )

            metadata = payment_intent.metadata
            participants = pending_booking.participants
            initial_instance = pending_booking.schedule_instance

            # Capacity Check (legacy pending booking)
            other_participants = (
                initial_instance.bookings.filter(status__in=["confirmed", "pending"])
                .exclude(pk=pending_booking.pk)
                .aggregate(total=Coalesce(Sum("participants"), 0))["total"]
            )
            if (initial_instance.max_participants - other_participants) < participants:
                raise DRFValidationError(
                    f"Session on {initial_instance.date.strftime('%b %d')} is now full."
                )

            # Safeguard: never confirm a booking with placeholder guest details
            if pending_booking.contact and not pending_booking.user:
                if is_placeholder_guest_contact(pending_booking.contact):
                    raise DRFValidationError(
                        "Guest contact details are invalid. Initiating refund."
                    )

            # --- CALCULATE FEES AND NET PAYOUT (use subtotal_for_payout so business never loses from global discount) ---
            grand_total = Decimal(payment_intent.amount_received) / 100
            total_tax = Decimal(metadata.get("tax_amount", "0.00"))
            subtotal_for_payout = Decimal(
                metadata.get("subtotal_for_payout")
                or metadata.get("subtotal_after_discount", "0.00")
            )
            business = initial_instance.schedule.option.classId.businessId

            if metadata.get("booking_source") == "widget":
                plan_id = (metadata.get("plan_id") or "basic").lower()
                fee_percentage = {
                    "basic": Decimal("4.00"),
                    "growth": Decimal("3.00"),
                    "advanced": Decimal("2.00"),
                }.get(plan_id, Decimal("4.00"))
                logger.info(
                    f"[{webhook_id}] Applying widget plan fee ({fee_percentage}%) for booking {pending_booking.id}."
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
            platform_fee_amount = (subtotal_for_payout * service_fee_rate).quantize(
                Decimal("0.01")
            )
            platform_fee_tax = (platform_fee_amount * HST_RATE).quantize(
                Decimal("0.01")
            )
            business_payout_tax = total_tax - platform_fee_tax
            business_net_revenue = subtotal_for_payout - platform_fee_amount
            net_payout_to_business = business_net_revenue + business_payout_tax

            # --- UPDATE PENDING BOOKING TO CONFIRMED ---
            pending_booking.status = "confirmed"
            pending_booking.payment_status = "paid"
            # FIX: Ensure the booking actually knows how much it's worth to the business
            pending_booking.allocated_net_payout = net_payout_to_business

            if not pending_booking.user_facing_reference:
                pending_booking.user_facing_reference = (
                    pending_booking._generate_user_facing_reference()
                )

            if pending_booking.contact and not pending_booking.user:
                pending_booking.cancellation_token = uuid.uuid4()
                logger.info(
                    f"[{webhook_id}] Generated cancellation token for guest booking {pending_booking.id}"
                )

            pending_booking.save()
            logger.info(
                f"[{webhook_id}] Booking {pending_booking.id} confirmed with Net Payout: {net_payout_to_business}."
            )

            # --- Handle Discount Redemption (Single Session) ---
            applied_discount_id = metadata.get("applied_discount_id")
            discount_amount = Decimal(metadata.get("discount_amount", "0.00"))

            if applied_discount_id:
                try:
                    discount = Discount.objects.select_for_update().get(
                        pk=applied_discount_id
                    )
                    discount.redeem()  # Increments usage_count atomically

                    AppliedDiscount.objects.create(
                        booking=pending_booking,
                        discount=discount,
                        amount_saved=discount_amount,
                    )
                    logger.info(
                        f"[{webhook_id}] Redeemed discount {discount.code} for booking {pending_booking.id}"
                    )
                except Discount.DoesNotExist:
                    logger.warning(
                        f"[{webhook_id}] Discount {applied_discount_id} not found during webhook processing."
                    )

            # --- Handle Global Discount Redemption (Single Session) ---
            global_discount_id = metadata.get("global_discount_id")
            global_discount_amount = Decimal(
                metadata.get("global_discount_amount", "0.00")
            )
            if global_discount_id and global_discount_amount > 0:
                try:
                    g_discount = GlobalDiscount.objects.select_for_update().get(
                        pk=global_discount_id
                    )
                    g_discount.redeem()
                    AppliedGlobalDiscount.objects.create(
                        booking=pending_booking,
                        global_discount=g_discount,
                        amount_saved=global_discount_amount,
                    )
                    logger.info(
                        f"[{webhook_id}] Redeemed global discount {g_discount.name} for booking {pending_booking.id}"
                    )
                except GlobalDiscount.DoesNotExist:
                    logger.warning(
                        f"[{webhook_id}] Global discount ID {global_discount_id} not found in DB."
                    )

            # --- Handle Gift Card Deduction (Step D - Single Session) ---
            gc_code = metadata.get("gift_card_code")
            gc_amount_str = metadata.get("gift_card_amount_to_deduct")

            if gc_code and gc_amount_str:
                try:
                    gc_amount = Decimal(gc_amount_str)
                    if gc_amount > 0:
                        gc = GiftCard.objects.select_for_update().get(code=gc_code)
                        if gc.current_balance >= gc_amount:
                            gc.current_balance -= gc_amount
                            gc.save()

                            GiftCardTransaction.objects.create(
                                gift_card=gc,
                                booking=pending_booking,
                                amount=-gc_amount,
                                balance_after=gc.current_balance,
                                transaction_type="redemption",
                            )
                            logger.info(
                                f"[{webhook_id}] Deducted ${gc_amount} from GC {gc_code} for single session."
                            )
                        else:
                            logger.critical(
                                f"[{webhook_id}] Insufficient funds in GC {gc_code} for committed transaction. Manual review required."
                            )
                except Exception as e:
                    logger.error(
                        f"[{webhook_id}] Failed to process GC deduction: {e}",
                        exc_info=True,
                    )

            # --- UPDATE PAYMENT RECORD ---
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

        # --- EMAILS AND NOTIFICATIONS ---
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

        if business_sms_enabled(business):
            booker = recipient_user or recipient_contact
            phone = getattr(booker, "phone_number", None) if booker else None
            if not phone and pending_booking.metadata:
                phone = pending_booking.metadata.get("guest_phone") or ""
            normalized = normalize_phone_for_sns(phone or "")
            if normalized:
                class_title = getattr(pending_booking.schedule_instance.schedule.option.classId, "title", "Class")
                date_str = pending_booking.schedule_instance.date.strftime("%b %d") if pending_booking.schedule_instance and pending_booking.schedule_instance.date else ""
                try:
                    send_sms_task.delay(normalized, f"You're booked for {class_title} on {date_str}. ClassEasily")
                except Exception as sms_e:
                    logger.warning("Booking confirmation SMS failed: %s", sms_e)

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

            for recipient in recipients:
                if recipient and recipient.email:
                    send_business_new_booking_email(recipient, pending_booking)

            if business_sms_enabled(business):
                class_title = getattr(pending_booking.schedule_instance.schedule.option.classId, "title", "Class")
                date_str = pending_booking.schedule_instance.date.strftime("%b %d") if pending_booking.schedule_instance and pending_booking.schedule_instance.date else ""
                sms_msg = f"New booking: {class_title} on {date_str}. ClassEasily"
                for r in recipients:
                    if r:
                        normalized = normalize_phone_for_sns(getattr(r, "phone_number", None) or "")
                        if normalized:
                            try:
                                send_sms_task.delay(normalized, sms_msg)
                            except Exception as sms_e:
                                logger.warning("New booking SMS failed: %s", sms_e)

            send_super_admin_booking_created_email(pending_booking)

        # Meta CAPI: server-side Purchase with deduplication (event_id = booking_id)
        # Use fbc/fbp from PaymentIntent metadata (stored at create-payment-intent) for paid conversions
        try:
            send_purchase_event_for_booking(
                pending_booking,
                value=float(grand_total),
                currency="CAD",
                num_items=pending_booking.participants or 1,
                meta_fbc=metadata.get("meta_fbc") or None,
                meta_fbp=metadata.get("meta_fbp") or None,
            )
        except Exception as capi_err:
            logger.warning(
                "[%s] Meta CAPI send failed (non-fatal): %s",
                webhook_id,
                capi_err,
            )
        _revalidate_for_booking(pending_booking)
        try:
            grand_total = Decimal(payment_intent.amount_received) / 100
            try_send_first_purchase_gift_card(
                dict(metadata),
                grand_total,
                payment_intent_id=payment_intent.id,
            )
        except Exception as fp_err:
            logger.warning(
                "[%s] First-purchase gift card failed (non-fatal): %s",
                webhook_id, fp_err, exc_info=True,
            )
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


class CancelPendingBookingView(APIView):
    permission_classes = []  # Allow guests

    def post(self, request):
        payment_intent_id = request.data.get("payment_intent_id")

        if not payment_intent_id:
            return Response(
                {"error": "ID required"}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            with transaction.atomic():
                # Find the pending payment
                payment = (
                    Payment.objects.select_related("booking")
                    .filter(
                        stripe_payment_intent_id=payment_intent_id, status="pending"
                    )
                    .first()
                )

                if payment:
                    # 1. Cancel Stripe Intent so the hold on the card (if any) is released
                    try:
                        stripe.PaymentIntent.cancel(payment_intent_id)
                    except stripe.error.StripeError as e:
                        # It might already be cancelled or succeeded, log and move on
                        logger.warning(
                            f"Stripe cancel failed for {payment_intent_id}: {e}"
                        )

                    # 2. HARD DELETE records instead of marking as cancelled
                    booking = payment.booking

                    if booking:
                        _revalidate_for_booking(booking)
                        # If it's a course, we need to clean up the whole group and enrollment
                        if booking.booking_group_id:
                            # Delete Enrollment (Parent)
                            CourseEnrollment.objects.filter(
                                booking_group_id=booking.booking_group_id
                            ).delete()

                            # Delete all bookings in this group
                            Booking.objects.filter(
                                booking_group_id=booking.booking_group_id
                            ).delete()
                        else:
                            # Delete Single Session Booking
                            booking.delete()

                    # Note: payment.delete() happens automatically via CASCADE if booking is deleted,
                    # but if payment existed without booking (orphaned), delete it explicitly:
                    if Payment.objects.filter(id=payment.id).exists():
                        payment.delete()

                    logger.info(
                        f"Hard deleted pending booking resources for PI {payment_intent_id}"
                    )

            return Response({"status": "cancelled"}, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error cancelling booking {payment_intent_id}: {e}")
            return Response(
                {"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
