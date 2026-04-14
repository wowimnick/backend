# quickstart/views/widget/widget_views.py
import uuid
import stripe
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, generics
from rest_framework.exceptions import ValidationError, NotFound
from django.db.models import Q, Count, Sum, Subquery, OuterRef, IntegerField, Prefetch
from django.db.models.functions import Coalesce

from quickstart.utils.stripe_processing_fee import estimate_stripe_processing_fee
from quickstart.models import (
    BusinessInfo,
    BusinessStaff,
    ClassesMain,
    ScheduleInstance,
    Booking,
    Contact,
    CustomerMembership,
    Payment,
    ClassImage,
    ClassOption,
    WidgetSubscription,
    WidgetFunnelEvent,
    Discount,
    AppliedDiscount,
    MembershipProduct,
)

WIDGET_FUNNEL_PERSIST_EVENTS = frozenset(
    {
        "widget_opened",
        "step_class",
        "step_option",
        "step_calendar",
        "step_checkout",
        "step_payment_started",
        "booking_completed",
    }
)
from quickstart.services.membership_service import (
    create_approval_membership,
    create_customer_membership_subscription,
    get_credits_remaining,
    consume_credit,
)
from quickstart.utils.stripe_metadata import stripe_metadata_to_dict
from quickstart.serializers.widget.widget_serializers import (
    WidgetBusinessConfigSerializer,
    WidgetClassSerializer,
    WidgetScheduleInstanceSerializer,
    GuestBookingCreateSerializer,
    GuestFreeBookingCreateSerializer,
)

from quickstart.utils.email_utils import (
    send_booking_confirmation_email,
    send_business_new_booking_email,
    send_super_admin_booking_created_email,
)
from quickstart.utils.sms_utils import business_sms_enabled, normalize_phone_for_sns
from quickstart.tasks.notification_tasks import send_sms_task
from quickstart.utils.permissions import IsValidWidgetRequest
from quickstart.utils.widget_throttle import WidgetRateThrottle

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY

# Match quickstart.payments.views HST for payout splits (platform fee tax vs business tax share)
HST_RATE = Decimal("0.13")


def _get_receipt_url_from_pi(pi):
    """Safely get receipt_url from a PaymentIntent; PI may not have charges expanded or may be refunded."""
    charges = getattr(pi, "charges", None)
    if not charges:
        return None
    data = getattr(charges, "data", None)
    if not data or len(data) == 0:
        return None
    return getattr(data[0], "receipt_url", None)


def _is_demo(request):
    """True when X-Business-ID is the reserved demo key; no DB business, mock data only."""
    return getattr(request.business_context, "is_demo", False)


def _persist_widget_funnel_event(request, event_name):
    """Best-effort insert; never raises to the client."""
    if _is_demo(request):
        return
    data = request.data
    session_id = (data.get("widget_session_id") or "").strip()
    if not session_id:
        return
    session_id = session_id[:64]
    step_raw = data.get("step") or ""
    step = str(step_raw)[:30]
    device_raw = data.get("device_type") or ""
    device_type = str(device_raw)[:10]
    class_id = data.get("class_id")
    cid = None
    if class_id is not None and str(class_id).strip() != "":
        try:
            cid = int(class_id)
        except (TypeError, ValueError):
            cid = None
    try:
        WidgetFunnelEvent.objects.create(
            business=request.business_context,
            session_id=session_id,
            event=str(event_name)[:50],
            step=step,
            device_type=device_type,
            class_id=cid,
        )
    except Exception:
        logger.warning(
            "Failed to persist widget funnel event event=%s",
            event_name,
            exc_info=True,
        )


def _business_has_active_widget_subscription(business):
    """True if widget subscription is not required, or business has an active subscription."""
    if getattr(business, "is_demo", False):
        return True
    if not getattr(settings, "WIDGET_SUBSCRIPTION_REQUIRED", False):
        return True
    from quickstart.services.widget_subscription_service import get_widget_subscription

    sub = get_widget_subscription(business)
    if not sub:
        return False
    now = timezone.now()
    return (sub.status or "").strip().lower() in ("active", "trialing") and (
        sub.current_period_end is None or sub.current_period_end > now
    )


def _business_has_growth_or_advanced_widget_plan(business):
    """Delegate to shared helper (booking analytics, revenue, emails, widget APIs)."""
    from quickstart.utils.widget_booking_source import (
        business_has_growth_or_advanced_widget_plan,
    )

    return business_has_growth_or_advanced_widget_plan(business)


# Commission by plan: basic=4%, growth=3%, advanced=2%
WIDGET_PLAN_FEE_PERCENT = {"basic": Decimal("4.00"), "growth": Decimal("3.00"), "advanced": Decimal("2.00")}


def _get_widget_plan_fee_percentage(business):
    """Return fee percentage (Decimal) for business's active widget plan; default 4% if none."""
    if getattr(business, "is_demo", False):
        return Decimal("4.00")
    now = timezone.now()
    sub = (
        WidgetSubscription.objects.filter(
            business=business,
            status__in=["active", "trialing"],
        )
        .filter(
            Q(current_period_end__isnull=True) | Q(current_period_end__gt=now)
        )
        .order_by("-current_period_end")
        .first()
    )
    if not sub or not sub.plan_id:
        return Decimal("4.00")
    return WIDGET_PLAN_FEE_PERCENT.get((sub.plan_id or "").lower(), Decimal("4.00"))


def _get_active_membership_for_booking(business, email, class_id):
    """
    If the given email has an active membership for this business that covers the class,
    return (CustomerMembership, use_credits). Otherwise return (None, False).
    use_credits: True when access_type is credits (caller must check credits_remaining).
    When multiple memberships match, prefer a credit-based plan with remaining balance,
    otherwise an unlimited (non-credit) plan.
    """
    if not email or getattr(business, "is_demo", False):
        return None, False
    try:
        contact = Contact.objects.get(business=business, email__iexact=email.strip())
    except Contact.DoesNotExist:
        return None, False
    now = timezone.now()
    memberships = (
        CustomerMembership.objects.filter(
            contact=contact,
            product__business=business,
            status__in=["active", "trialing"],
        )
        .filter(
            Q(current_period_end__isnull=True) | Q(current_period_end__gt=now)
        )
        .select_related("product")
        .prefetch_related("product__applicable_classes")
    )
    candidates = []
    for membership in memberships:
        product = membership.product
        if not product.is_active:
            continue
        applicable = product.applicable_classes.all()
        if applicable.exists() and not applicable.filter(classId=class_id).exists():
            continue
        candidates.append(membership)
    if not candidates:
        return None, False
    credit_eligible = [
        m
        for m in candidates
        if m.product.access_type == "credits" and m.product.credit_allowance
    ]
    for m in credit_eligible:
        remaining = get_credits_remaining(m)
        if remaining is not None and remaining > 0:
            return m, True
    non_credit = [
        m
        for m in candidates
        if not (m.product.access_type == "credits" and m.product.credit_allowance)
    ]
    if non_credit:
        return non_credit[0], False
    return None, False


def _get_widget_plan_id(business):
    """Return plan_id for business's active widget subscription, or None."""
    if getattr(business, "is_demo", False):
        return "basic"
    now = timezone.now()
    sub = (
        WidgetSubscription.objects.filter(
            business=business,
            status__in=["active", "trialing"],
        )
        .filter(
            Q(current_period_end__isnull=True) | Q(current_period_end__gt=now)
        )
        .order_by("-current_period_end")
        .first()
    )
    return (sub.plan_id or "").lower() if sub else None


def _widget_validate_and_cap_discount_for_session(
    business, instance, participants, applied_discount_id, client_discount_amount
):
    """
    Same validation as CreateGuestPaymentIntentView discount block.
    Returns capped discount (Decimal) for the session subtotal.
    """
    subtotal = instance.price * Decimal(participants)
    try:
        discount_amount = Decimal(str(client_discount_amount or 0))
    except (TypeError, ValueError, InvalidOperation):
        raise ValidationError("Invalid discount amount.")
    if not applied_discount_id or discount_amount <= 0:
        raise ValidationError("Invalid or expired discount.")
    try:
        discount = Discount.objects.get(id=applied_discount_id, business=business)
    except (Discount.DoesNotExist, ValueError):
        raise ValidationError("Invalid or expired discount.")
    if not discount.apply_to_widget:
        raise ValidationError("This discount is not valid for widget bookings.")
    if not discount.is_active:
        raise ValidationError("This coupon is currently inactive.")
    now = timezone.now()
    if discount.valid_from and now < discount.valid_from:
        raise ValidationError("This coupon is not yet active.")
    if discount.valid_to and now > discount.valid_to:
        raise ValidationError("This coupon has expired.")
    if discount.usage_limit is not None:
        current_usage = Booking.objects.filter(discounts=discount).count()
        if current_usage >= discount.usage_limit:
            raise ValidationError("This coupon has reached its usage limit.")
    if (
        discount.min_purchase_amount is not None
        and subtotal < discount.min_purchase_amount
    ):
        raise ValidationError(
            f"A minimum purchase of ${discount.min_purchase_amount:.2f} is required."
        )
    option = instance.schedule.option
    if discount.scope == "class" and discount.target_class_id != option.classId_id:
        raise ValidationError("This coupon is not valid for the selected class.")
    if discount.scope == "schedule_group" and (
        discount.target_class_option_id != option.id
        or not discount.target_schedule_group_name
    ):
        raise ValidationError("This coupon is not valid for the selected session.")
    return min(discount_amount, subtotal).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


class WidgetEventsView(APIView):
    """
    Accepts widget analytics/error events (loaded, booking_completed, error).
    No PII. Rate limited. Logs for support/analytics.
    """

    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]

    def post(self, request, *args, **kwargs):
        event = request.data.get("event") or ""
        message = request.data.get("message", "")
        component_stack = request.data.get("component_stack", "")
        business_id = getattr(request.business_context, "businessId", None)
        logger.info(
            "Widget event: event=%s business_id=%s message=%s",
            event,
            business_id,
            (message or "")[:200],
        )
        if event == "error" and (message or component_stack):
            logger.warning(
                "Widget error report: business_id=%s message=%s stack=%s",
                business_id,
                message[:500] if message else "",
                (component_stack[:500] if component_stack else ""),
            )
        if event in WIDGET_FUNNEL_PERSIST_EVENTS:
            _persist_widget_funnel_event(request, event)
        return Response(status=status.HTTP_204_NO_CONTENT)


class WidgetConfigView(generics.RetrieveAPIView):
    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]
    serializer_class = WidgetBusinessConfigSerializer

    def get_object(self):
        return self.request.business_context

    def retrieve(self, request, *args, **kwargs):
        if _is_demo(request):
            base = getattr(settings, "FRONTEND_BASE_URL", "https://www.classeasily.com").rstrip("/")
            return Response({
                "businessName": "Demo Business",
                "business_timezone": "America/Los_Angeles",
                "currency": "USD",
                "theme": {
                    "view": "modal",
                    "primaryColor": "#2563eb",
                    "backgroundColor": "#ffffff",
                    "fontFamily": "inherit",
                },
                "widget_fee_percentage": 4.0,
                "stripe_publishable_key": getattr(settings, "STRIPE_PUBLIC_KEY", ""),
                "terms_url": f"{base}/terms-of-service",
                "privacy_url": f"{base}/privacy-policy",
            })
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
        # Pin widget to a specific class is Growth/Advanced only; strip for Basic so embed does not use it.
        if not _business_has_growth_or_advanced_widget_plan(instance) and isinstance(
            data.get("theme"), dict
        ):
            theme = dict(data["theme"])
            theme.pop("specificClassId", None)
            data["theme"] = theme
        data["stripe_publishable_key"] = settings.STRIPE_PUBLIC_KEY
        base = getattr(settings, "FRONTEND_BASE_URL", "https://www.classeasily.com").rstrip("/")
        data["terms_url"] = f"{base}/terms-of-service"
        data["privacy_url"] = f"{base}/privacy-policy"
        return Response(data)


# Mock option ID used for demo availability; must match mock class below.
DEMO_OPTION_ID = "demo-option-1"

# Fake instance IDs for demo slots (widget only displays; booking is disabled).
DEMO_INSTANCE_ID_BASE = 90000


def _get_demo_availability(start_date_str, end_date_str):
    """Build mock availability by date for demo option (next 4–8 weeks, a few times per day)."""
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except ValueError:
        return {}
    if start > end:
        return {}
    # Cap range for demo
    today = timezone.now().date()
    start = max(start, today)
    end = min(end, today + timedelta(days=56))
    availability_by_date = {}
    slot_times = ["09:00:00", "14:00:00", "18:00:00"]
    instance_id = DEMO_INSTANCE_ID_BASE
    d = start
    while d <= end:
        availability_by_date[d.isoformat()] = [
            {
                "instance_id": instance_id + i,
                "time": slot_times[i % len(slot_times)],
                "duration": 120,
                "price": "49.00",
                "max_participants": 8,
                "available_spots": 6,
                "min_participants": 1,
            }
            for i in range(len(slot_times))
        ]
        instance_id += len(slot_times)
        d += timedelta(days=1)
    return availability_by_date


def _get_demo_classes_payload():
    """Single mock class with one option for demo mode."""
    return [
        {
            "classId": "demo-class-1",
            "title": "Sunset Paddleboard Tour",
            "description": "A relaxing guided tour along the coast. No experience required.",
            "options": [
                {
                    "optionId": DEMO_OPTION_ID,
                    "booking_type": "group",
                    "level": "all",
                    "schedules": [
                        {"id": 1, "duration": 120, "price": "49.00", "maxParticipants": 8},
                    ],
                    "cancellation_policy": "standard",
                    "cancellation_refund_percentage": 100,
                    "cancellation_custom_hours": 24,
                },
            ],
            "images": [],
            "average_rating": 4.5,
            "review_count": 12,
            "require_participant_names": False,
            "location": "123 Demo Street",
            "location_name": "Main studio",
            "location_address": "123 Demo Street",
        },
    ]


class WidgetClassListView(generics.ListAPIView):
    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]
    serializer_class = WidgetClassSerializer

    def list(self, request, *args, **kwargs):
        if _is_demo(request):
            return Response(_get_demo_classes_payload())
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        return (
            ClassesMain.objects.filter(
                businessId=self.request.business_context, status="active"
            )
            .select_related("businessId", "location_ref")
            .prefetch_related(
                "options__schedules",
                Prefetch("images", queryset=ClassImage.objects.order_by("-isCover")),
            )
        )


class WidgetAvailabilityView(APIView):
    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]

    def get(self, request, *args, **kwargs):
        logger.info(f"Widget availability request: {request.query_params}")

        option_id = request.query_params.get("option_id")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        if not all([option_id, start_date, end_date]):
            raise ValidationError(
                "`option_id`, `start_date`, and `end_date` are required."
            )

        if _is_demo(request):
            if option_id != DEMO_OPTION_ID:
                raise ValidationError("Invalid option_id for this business.")
            return Response(_get_demo_availability(start_date, end_date))

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


class ValidateWidgetCouponView(APIView):
    """
    Validate a coupon for widget checkout. Requires code, schedule_instance_id, base_amount.
    Returns discount id and calculated_discount_amount; only discounts with apply_to_widget=True are valid.
    """

    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]

    def post(self, request, *args, **kwargs):
        if _is_demo(request):
            return Response(
                {"error": "demo_mode", "message": "Demo mode."},
                status=status.HTTP_403_FORBIDDEN,
            )
        business = request.business_context
        code = request.data.get("code")
        base_amount_str = request.data.get("base_amount")
        schedule_instance_id = request.data.get("schedule_instance_id")
        if not all([code, base_amount_str, schedule_instance_id]):
            return Response(
                {"detail": "code, base_amount, and schedule_instance_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            base_amount = Decimal(str(base_amount_str))
            instance = ScheduleInstance.objects.select_related(
                "schedule__option__classId"
            ).get(id=schedule_instance_id, schedule__option__classId__businessId=business)
        except (ScheduleInstance.DoesNotExist, ValueError, TypeError):
            return Response(
                {"detail": "Invalid schedule instance or amount."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        option = instance.schedule.option
        coupon_code_upper = code.strip().upper()
        try:
            discount = Discount.objects.get(business=business, code=coupon_code_upper)
        except Discount.DoesNotExist:
            return Response(
                {"detail": "This coupon code is not valid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not discount.apply_to_widget:
            return Response(
                {"detail": "This code is not valid for widget bookings."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not discount.is_active:
            return Response(
                {"detail": "This coupon is currently inactive."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        now = timezone.now()
        if discount.valid_from and now < discount.valid_from:
            return Response(
                {"detail": "This coupon is not yet active."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if discount.valid_to and now > discount.valid_to:
            return Response(
                {"detail": "This coupon has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if discount.usage_limit is not None:
            current_usage = Booking.objects.filter(discounts=discount).count()
            if current_usage >= discount.usage_limit:
                return Response(
                    {"detail": "This coupon has reached its usage limit."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        if (
            discount.min_purchase_amount is not None
            and base_amount < discount.min_purchase_amount
        ):
            return Response(
                {
                    "detail": f"A minimum purchase of ${discount.min_purchase_amount:.2f} is required to use this coupon."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if discount.scope == "class" and discount.target_class_id != option.classId_id:
            return Response(
                {"detail": "This coupon is not valid for the selected class."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if discount.scope == "schedule_group" and (
            discount.target_class_option_id != option.id
            or not discount.target_schedule_group_name
        ):
            return Response(
                {"detail": "This coupon is not valid for the selected session."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        calculated_discount = Decimal("0.00")
        if discount.discount_type == "percentage":
            calculated_discount = (
                base_amount * (discount.value / Decimal(100))
            ).quantize(Decimal("0.01"))
        elif discount.discount_type == "fixed_amount":
            calculated_discount = discount.value
        calculated_discount = min(base_amount, calculated_discount)
        return Response(
            {
                "id": str(discount.id),
                "code": discount.code,
                "name": discount.name,
                "discount_type": discount.discount_type,
                "value": float(discount.value),
                "calculated_discount_amount": float(calculated_discount),
            },
            status=status.HTTP_200_OK,
        )


class CreateGuestPaymentIntentView(APIView):
    """
    Backend-authoritative price calculation and Payment Intent creation.
    """

    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]

    def post(self, request, *args, **kwargs):
        if _is_demo(request):
            return Response(
                {
                    "error": "demo_mode",
                    "message": "Demo mode: booking and payment are disabled.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
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

        # Member entitlement: if guest has active membership covering this class, charge $0
        guest_email = (request.data.get("email") or "").strip()
        membership = None
        member_booking = False
        if guest_email:
            membership, use_credits = _get_active_membership_for_booking(
                business,
                guest_email,
                instance.schedule.option.classId_id,
            )
            if membership:
                if use_credits:
                    remaining = get_credits_remaining(membership)
                    if remaining is not None and remaining <= 0:
                        membership = None
                    else:
                        member_booking = True
                else:
                    member_booking = True

        if member_booking and membership:
            subtotal = Decimal("0.00")
            applied_discount_id = None
            discount_amount = Decimal("0.00")
        else:
            subtotal = instance.price * Decimal(participants)
            applied_discount_id = request.data.get("applied_discount_id")
        discount_amount = None
        if applied_discount_id:
            try:
                discount_amount = Decimal(str(request.data.get("discount_amount", 0)))
            except (TypeError, ValueError):
                discount_amount = Decimal("0.00")
            if discount_amount <= 0:
                applied_discount_id = None
                discount_amount = None

        if applied_discount_id and discount_amount is not None:
            try:
                discount = Discount.objects.get(
                    id=applied_discount_id, business=business
                )
            except (Discount.DoesNotExist, ValueError):
                raise ValidationError("Invalid or expired discount.")
            if not discount.apply_to_widget:
                raise ValidationError("This discount is not valid for widget bookings.")
            if not discount.is_active:
                raise ValidationError("This coupon is currently inactive.")
            now = timezone.now()
            if discount.valid_from and now < discount.valid_from:
                raise ValidationError("This coupon is not yet active.")
            if discount.valid_to and now > discount.valid_to:
                raise ValidationError("This coupon has expired.")
            if discount.usage_limit is not None:
                current_usage = Booking.objects.filter(discounts=discount).count()
                if current_usage >= discount.usage_limit:
                    raise ValidationError("This coupon has reached its usage limit.")
            if (
                discount.min_purchase_amount is not None
                and subtotal < discount.min_purchase_amount
            ):
                raise ValidationError(
                    f"A minimum purchase of ${discount.min_purchase_amount:.2f} is required."
                )
            option = instance.schedule.option
            if discount.scope == "class" and discount.target_class_id != option.classId_id:
                raise ValidationError("This coupon is not valid for the selected class.")
            if discount.scope == "schedule_group" and (
                discount.target_class_option_id != option.id
                or not discount.target_schedule_group_name
            ):
                raise ValidationError("This coupon is not valid for the selected session.")
            discount_amount = min(discount_amount, subtotal)
        else:
            applied_discount_id = None
            discount_amount = Decimal("0.00")

        subtotal_for_payout = (subtotal - discount_amount).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if subtotal_for_payout < 0:
            subtotal_for_payout = Decimal("0.00")

        # Fee calculation: plan-based commission on post-discount subtotal
        plan_id = _get_widget_plan_id(business)
        fee_percentage = _get_widget_plan_fee_percentage(business)
        platform_fee = (
            subtotal_for_payout * (fee_percentage / Decimal("100"))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        hst_rate = Decimal("0.13")
        tax_on_subtotal = (subtotal_for_payout * hst_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        total_amount_charged = subtotal_for_payout + platform_fee + tax_on_subtotal
        net_payout_amount = subtotal_for_payout - platform_fee
        final_amount_cents = int(total_amount_charged * 100)

        metadata = {
            "business_id": business.businessId,
            "schedule_instance_id": instance.id,
            "participants": participants,
            "booking_source": "member_widget" if (member_booking and membership) else "widget",
            "plan_id": plan_id or "basic",
            "subtotal_cents": int(subtotal * 100),
            "subtotal_for_payout": str(subtotal_for_payout),
            "tax_cents": int(tax_on_subtotal * 100),
            # Webhook instant booking reads tax_amount (string dollars); widget previously only had tax_cents
            "tax_amount": str(tax_on_subtotal),
            "platform_fee_cents": int(platform_fee * 100),
            "net_payout_cents": int(net_payout_amount * 100),
        }
        if member_booking and membership:
            metadata["membership_id"] = str(membership.id)
        if applied_discount_id:
            metadata["applied_discount_id"] = str(applied_discount_id)
            metadata["discount_amount"] = str(discount_amount)

        if final_amount_cents <= 0:
            if member_booking and membership:
                return Response(
                    {"free_member_booking": True},
                    status=status.HTTP_200_OK,
                )
            # 100% coupon (or edge case): no Stripe charge; client uses GuestFreeBookingCreateView
            return Response(
                {"free_member_booking": False, "free_coupon_booking": True},
                status=status.HTTP_200_OK,
            )

        try:
            payment_intent = stripe.PaymentIntent.create(
                amount=final_amount_cents,
                currency=business.currency.lower(),
                automatic_payment_methods={"enabled": True},
                transfer_group=f"booking_widget_{uuid.uuid4()}",
                metadata=metadata,
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
    throttle_classes = [WidgetRateThrottle]
    serializer_class = GuestBookingCreateSerializer

    def create(self, request, *args, **kwargs):
        if _is_demo(request):
            return Response(
                {
                    "error": "demo_mode",
                    "message": "Demo mode: booking is disabled.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        business = request.business_context

        try:
            pi = stripe.PaymentIntent.retrieve(
                data["payment_intent_id"],
                expand=["charges.data"],
            )
            if pi.status != "succeeded":
                raise ValidationError(
                    f"Payment was not successful. Status: {pi.status}"
                )

            metadata = stripe_metadata_to_dict(pi.metadata)
            instance_id = int(metadata["schedule_instance_id"])
            participants = int(metadata["participants"])

            # Webhook may have already created the booking (instant flow); return it idempotently
            # to avoid duplicate bookings and deadlock with the webhook.
            existing_payment = (
                Payment.objects.filter(stripe_payment_intent_id=pi.id)
                .select_related("booking")
                .first()
            )
            if existing_payment and existing_payment.booking_id:
                return Response(
                    {
                        "message": "Booking confirmed!",
                        "booking_reference": existing_payment.booking.user_facing_reference,
                    },
                    status=status.HTTP_200_OK,
                )

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
                amount_charged = Decimal(pi.amount_received) / 100
                stripe_processing_fee = estimate_stripe_processing_fee(amount_charged)
                subtotal_for_payout = Decimal(
                    metadata.get("subtotal_for_payout")
                    or metadata.get("subtotal_after_discount", "0.00")
                )
                platform_fee_amount = Decimal(metadata.get("platform_fee_cents", 0)) / 100
                tax_amount = Decimal(metadata.get("tax_cents", 0)) / 100

                platform_fee_tax = (platform_fee_amount * HST_RATE).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                business_payout_tax = (tax_amount - platform_fee_tax).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                business_net_revenue = (
                    subtotal_for_payout - platform_fee_amount - stripe_processing_fee
                )
                net_payout_after_stripe = max(
                    Decimal("0.00"),
                    (business_net_revenue + business_payout_tax).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    ),
                )

                booking = Booking.objects.create(
                    contact=contact,
                    schedule_instance=instance,
                    participants=participants,
                    amount_paid=amount_charged,
                    status="confirmed",
                    payment_status="paid",
                    payout_status="pending",
                    enrollment_type=class_option.booking_type,
                    cancellation_policy=class_option.cancellationPolicy,
                    cancellation_custom_hours=class_option.cancellationCustomHours,
                    cancellation_refund_percentage=class_option.cancellationRefundPercentage,
                    cancellation_token=uuid.uuid4(),
                    allocated_net_payout=net_payout_after_stripe,
                )

                Payment.objects.create(
                    booking=booking,
                    stripe_payment_intent_id=pi.id,
                    stripe_charge_id=pi.latest_charge,
                    status="succeeded",
                    amount=amount_charged,
                    tax_amount=tax_amount,
                    platform_fee_amount=platform_fee_amount,
                    platform_fee_tax=platform_fee_tax,
                    stripe_processing_fee=stripe_processing_fee,
                    net_payout_amount=net_payout_after_stripe,
                    currency=business.currency,
                    payment_method_type=(
                        pi.payment_method_types[0]
                        if pi.payment_method_types
                        else "card"
                    ),
                    receipt_url=_get_receipt_url_from_pi(pi),
                    created_at=timezone.now(),
                    metadata={"original_stripe_metadata": dict(metadata)},
                )

                applied_discount_id = metadata.get("applied_discount_id")
                discount_amount_str = metadata.get("discount_amount")
                if applied_discount_id and discount_amount_str:
                    try:
                        discount = Discount.objects.select_for_update().get(
                            pk=applied_discount_id, business=business
                        )
                        discount_amount_val = Decimal(discount_amount_str)
                        discount.redeem()
                        AppliedDiscount.objects.create(
                            booking=booking,
                            discount=discount,
                            amount_saved=discount_amount_val,
                        )
                        logger.info(
                            "Widget: redeemed discount %s for booking %s",
                            discount.code,
                            booking.id,
                        )
                    except (Discount.DoesNotExist, ValueError) as e:
                        logger.warning(
                            "Widget: could not redeem discount %s for booking %s: %s",
                            applied_discount_id,
                            booking.id,
                            e,
                        )

                # Member booking: consume one credit if access_type is credits
                if metadata.get("booking_source") == "member_widget" and metadata.get("membership_id"):
                    try:
                        member = CustomerMembership.objects.get(
                            id=metadata["membership_id"],
                            product__business=business,
                        )
                        consume_credit(member, booking)
                    except (CustomerMembership.DoesNotExist, ValueError) as e:
                        logger.warning(
                            "Widget: could not consume credit for member booking %s: %s",
                            booking.id,
                            e,
                        )

            # --- Post-Transaction Actions: Emails (guest confirmation + business notification) ---
            try:
                send_booking_confirmation_email(
                    contact,
                    booking,
                    booking_source=metadata.get("booking_source", "widget"),
                )
            except Exception as email_err:
                logger.warning(
                    "Widget: failed to send guest confirmation email for booking %s: %s",
                    booking.id,
                    email_err,
                    exc_info=True,
                )
            if business_sms_enabled(business):
                phone = normalize_phone_for_sns((data.get("phone_number") or "").strip())
                if phone:
                    class_title = getattr(
                        instance.schedule.option.classId, "title", "Class"
                    )
                    date_str = (
                        instance.date.strftime("%b %d")
                        if instance.date
                        else ""
                    )
                    try:
                        send_sms_task.delay(
                            phone,
                            f"You're booked for {class_title} on {date_str}. ClassEasily",
                        )
                    except Exception as sms_e:
                        logger.warning(
                            "Widget: guest confirmation SMS failed for booking %s: %s",
                            booking.id,
                            sms_e,
                        )
            if getattr(business, "newBookingNotification", False):
                try:
                    recipients = {business.owner}
                    for staff in BusinessStaff.objects.filter(
                        business=business,
                        status="accepted",
                        role__permissions__codename="receive_booking_notifications",
                    ).select_related("user"):
                        if staff.user:
                            recipients.add(staff.user)
                    for r in recipients:
                        if r and getattr(r, "email", None):
                            send_business_new_booking_email(r, booking)
                except Exception as email_err:
                    logger.warning(
                        "Widget: failed to send business new-booking email for booking %s: %s",
                        booking.id,
                        email_err,
                        exc_info=True,
                    )
                try:
                    send_super_admin_booking_created_email(booking)
                except Exception as email_err:
                    logger.warning(
                        "Widget: failed to send super-admin new-booking email for booking %s: %s",
                        booking.id,
                        email_err,
                        exc_info=True,
                    )

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


class GuestFreeBookingCreateView(APIView):
    """
    Create a free (no payment) guest booking from the widget.
    Skips Stripe entirely; creates Contact, Booking, and internal Payment record.
    """

    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]

    def post(self, request, *args, **kwargs):
        if _is_demo(request):
            return Response(
                {"error": "demo_mode", "message": "Demo mode: booking is disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not _business_has_active_widget_subscription(request.business_context):
            return Response(
                {
                    "error": "widget_subscription_required",
                    "message": "An active widget subscription is required to accept bookings.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = GuestFreeBookingCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        business = request.business_context

        instance_id = data["schedule_instance_id"]
        participants = data["participants"]
        applied_discount_id = data.get("applied_discount_id")
        discount_amount = data.get("discount_amount") or Decimal("0.00")
        participant_details = data.get("participant_details") or []
        if not participant_details or len(participant_details) != participants:
            first_name = (data.get("first_name") or "").strip() or "Guest"
            participant_details = [{"name": first_name} for _ in range(participants)]

        membership = None
        member_booking = False
        try:
            with transaction.atomic():
                instance = ScheduleInstance.objects.select_for_update().get(
                    id=instance_id,
                    schedule__option__classId__businessId=business,
                )
                if not instance.can_accommodate(participants):
                    raise ValidationError(
                        "Not enough spots available. Only %s spots remain."
                        % instance.available_spots
                    )

                class_id = instance.schedule.option.classId_id
                session_subtotal = instance.price * Decimal(participants)
                if session_subtotal > 0:
                    membership, use_credits = _get_active_membership_for_booking(
                        business,
                        data["email"],
                        class_id,
                    )
                    if membership:
                        if use_credits:
                            rem = get_credits_remaining(membership)
                            if rem is None or rem <= 0:
                                raise ValidationError(
                                    "No credits remaining for this membership period."
                                )
                        member_booking = True
                    elif applied_discount_id:
                        capped_discount = _widget_validate_and_cap_discount_for_session(
                            business,
                            instance,
                            participants,
                            applied_discount_id,
                            discount_amount,
                        )
                        if capped_discount >= session_subtotal:
                            member_booking = False
                            discount_amount = capped_discount
                        else:
                            raise ValidationError(
                                "This session requires payment. Use the email on your active membership, or pay by card."
                            )
                    else:
                        raise ValidationError(
                            "This session requires payment. Use the email on your active membership, or pay by card."
                        )

                contact, _ = Contact.objects.get_or_create(
                    business=business,
                    email__iexact=data["email"],
                    defaults={
                        "first_name": data["first_name"],
                        "last_name": data.get("last_name", ""),
                        "phone_number": data.get("phone_number", ""),
                        "source": "widget_booking",
                    },
                )
                if not contact.first_name and data.get("first_name"):
                    contact.first_name = data["first_name"]
                if data.get("last_name") is not None:
                    contact.last_name = data.get("last_name", "")
                if data.get("phone_number") is not None:
                    contact.phone_number = data.get("phone_number", "")
                contact.save()

                option = instance.schedule.option
                booking = Booking.objects.create(
                    contact=contact,
                    schedule_instance=instance,
                    participants=participants,
                    participant_details=participant_details,
                    notes=data.get("notes", "") or "",
                    amount_paid=Decimal("0.00"),
                    status="confirmed",
                    payment_status="paid",
                    enrollment_type=option.booking_type,
                    cancellation_policy=option.cancellationPolicy,
                    cancellation_custom_hours=option.cancellationCustomHours,
                    cancellation_refund_percentage=option.cancellationRefundPercentage,
                    cancellation_token=uuid.uuid4(),
                )
                booking.user_facing_reference = booking._generate_user_facing_reference()
                booking.save(update_fields=["user_facing_reference"])

                pay_meta = {
                    "is_free": True,
                    "booking_source": "member_widget" if member_booking else "widget",
                    "applied_discount_id": str(applied_discount_id) if applied_discount_id else None,
                }
                if member_booking and membership:
                    pay_meta["membership_id"] = str(membership.id)
                Payment.objects.create(
                    booking=booking,
                    stripe_payment_intent_id=f"internal_{uuid.uuid4()}",
                    amount=Decimal("0.00"),
                    tax_amount=Decimal("0.00"),
                    platform_fee_amount=Decimal("0.00"),
                    net_payout_amount=Decimal("0.00"),
                    currency=business.currency,
                    status="succeeded",
                    metadata=pay_meta,
                )

                if member_booking and membership:
                    try:
                        consume_credit(membership, booking)
                    except ValueError as e:
                        raise ValidationError(str(e))

                if applied_discount_id and discount_amount > 0:
                    try:
                        discount = Discount.objects.select_for_update().get(
                            pk=applied_discount_id, business=business
                        )
                        if discount.apply_to_widget and discount.is_active:
                            discount.redeem()
                            AppliedDiscount.objects.create(
                                booking=booking,
                                discount=discount,
                                amount_saved=discount_amount,
                            )
                            logger.info(
                                "Widget free booking: redeemed discount %s for booking %s",
                                discount.code,
                                booking.id,
                            )
                    except (Discount.DoesNotExist, ValueError) as e:
                        logger.warning(
                            "Widget free booking: could not redeem discount %s: %s",
                            applied_discount_id,
                            e,
                        )

            # --- Emails (same as paid widget flow) ---
            try:
                send_booking_confirmation_email(
                    contact,
                    booking,
                    booking_source="member_widget" if member_booking else "widget",
                )
            except Exception as email_err:
                logger.warning(
                    "Widget free: failed to send guest confirmation for booking %s: %s",
                    booking.id,
                    email_err,
                    exc_info=True,
                )

            if getattr(business, "newBookingNotification", False):
                try:
                    recipients = {business.owner}
                    for staff in BusinessStaff.objects.filter(
                        business=business,
                        status="accepted",
                        role__permissions__codename="receive_booking_notifications",
                    ).select_related("user"):
                        if staff.user:
                            recipients.add(staff.user)
                    for r in recipients:
                        if r and getattr(r, "email", None):
                            send_business_new_booking_email(r, booking)
                except Exception as email_err:
                    logger.warning(
                        "Widget free: failed to send business new-booking email for %s: %s",
                        booking.id,
                        email_err,
                        exc_info=True,
                    )

            # --- SMS (guest confirmation + business new booking) ---
            si = booking.schedule_instance
            if business_sms_enabled(business) and si:
                phone = getattr(contact, "phone_number", None) or ""
                normalized = normalize_phone_for_sns(phone)
                if normalized:
                    class_title = getattr(si.schedule.option.classId, "title", "Class")
                    date_str = si.date.strftime("%b %d")
                    t = getattr(si, "time", None)
                    time_str = t.strftime("%I:%M %p").lstrip("0") if t and hasattr(t, "strftime") else (str(t) if t else "")
                    business_name = getattr(business, "businessName", "") or "ClassEasily"
                    sms_msg = (
                        f"You're in! {class_title} is on {date_str} at {time_str}.\n\n"
                        f"Add it to your calendar — we'll send a reminder the day before.\n\n— {business_name}"
                    )
                    try:
                        send_sms_task.delay(normalized, sms_msg)
                    except Exception as sms_e:
                        logger.warning("Widget free: guest confirmation SMS failed: %s", sms_e)
                if business.newBookingNotification:
                    recipients = {business.owner}
                    for staff in BusinessStaff.objects.filter(
                        business=business,
                        status="accepted",
                        role__permissions__codename="receive_booking_notifications",
                    ).select_related("user"):
                        if staff.user:
                            recipients.add(staff.user)
                    class_title = getattr(si.schedule.option.classId, "title", "Class")
                    date_str = si.date.strftime("%b %d")
                    t = getattr(si, "time", None)
                    time_str = t.strftime("%I:%M %p").lstrip("0") if t and hasattr(t, "strftime") else (str(t) if t else "")
                    booker_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or contact.email or "A customer"
                    sms_msg = (
                        f"New booking: {class_title} on {date_str} at {time_str}.\n\n"
                        f"Booked by {booker_name}. Check your dashboard for details.\n\n— ClassEasily"
                    )
                    for r in recipients:
                        if r:
                            ph = getattr(r, "phone_number", None) or ""
                            norm = normalize_phone_for_sns(ph)
                            if norm:
                                try:
                                    send_sms_task.delay(norm, sms_msg)
                                except Exception as sms_e:
                                    logger.warning("Widget free: new booking SMS failed: %s", sms_e)

            return Response(
                {
                    "message": "Booking confirmed!",
                    "booking_reference": booking.user_facing_reference,
                },
                status=status.HTTP_201_CREATED,
            )

        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                "Widget free booking: unhandled exception: %s",
                e,
                exc_info=True,
            )
            return Response(
                {"error": "An unexpected server error occurred. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class WidgetMembershipProductsView(APIView):
    """GET: List active membership products for the business (widget, no auth)."""

    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]

    def get(self, request):
        business = request.business_context
        if getattr(business, "is_demo", False):
            # Demo mode: return one mock product so the widget can show the membership flow.
            ce_plan = (request.query_params.get("ce_plan") or "").strip() or None
            mock_id = ce_plan if ce_plan else "00000000-0000-4000-8000-000000000001"
            return Response({
                "products": [
                    {
                        "id": mock_id,
                        "name": "Demo membership",
                        "badge_text": "",
                        "description": "Sample plan for widget preview. Subscriptions are disabled in demo.",
                        "price": "29.00",
                        "currency": "USD",
                        "billing_interval": "month",
                        "access_type": "unlimited",
                        "credit_allowance": None,
                        "credit_unit": "",
                        "requires_approval": False,
                        "application_instructions": "",
                        "signup_fields": [],
                        "confirmation_message": "",
                        "welcome_url": "",
                        "max_members": None,
                        "widget_button_config": {},
                        "widget_features": {},
                        "widget_cta_label": "",
                    }
                ]
            }, status=status.HTTP_200_OK)
        products = MembershipProduct.objects.filter(
            business=business, is_active=True
        ).order_by("price")
        out = []
        for p in products:
            out.append({
                "id": str(p.id),
                "name": p.name,
                "badge_text": getattr(p, "badge_text", "") or "",
                "description": p.description or "",
                "price": str(p.price),
                "currency": p.currency,
                "billing_interval": p.billing_interval,
                "access_type": p.access_type,
                "credit_allowance": p.credit_allowance,
                "credit_unit": p.credit_unit or "",
                "requires_approval": getattr(p, "requires_approval", False),
                "application_instructions": getattr(p, "application_instructions", "") or "",
                "signup_fields": getattr(p, "signup_fields", None) or [],
                "confirmation_message": getattr(p, "confirmation_message", "") or "",
                "welcome_url": getattr(p, "welcome_url", "") or "",
                "max_members": getattr(p, "max_members", None),
                "widget_button_config": getattr(p, "widget_button_config", None) or {},
                "widget_features": getattr(p, "widget_features", None) or {},
                "widget_cta_label": getattr(p, "widget_cta_label", "") or "",
            })
        return Response({"products": out}, status=status.HTTP_200_OK)


class WidgetMembershipSubscribeView(APIView):
    """POST: Create Stripe subscription for a customer membership; returns client_secret."""

    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]

    def post(self, request):
        if _is_demo(request):
            return Response(
                {"error": "demo_mode", "message": "Demo mode: subscriptions disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )
        business = request.business_context
        product_id = request.data.get("product_id")
        if not product_id:
            return Response(
                {"error": "product_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            product = MembershipProduct.objects.get(
                id=product_id, business=business, is_active=True
            )
        except (MembershipProduct.DoesNotExist, ValueError, TypeError):
            raise NotFound("Membership product not found.")
        email = (request.data.get("email") or "").strip()
        if not email:
            return Response(
                {"error": "email is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Block same email from subscribing again to this plan (active or trialing)
        try:
            contact = Contact.objects.get(business=business, email__iexact=email)
            if CustomerMembership.objects.filter(
                contact=contact,
                product=product,
                status__in=["active", "trialing"],
            ).exists():
                return Response(
                    {
                        "error": "already_subscribed",
                        "message": "This email is already subscribed to this plan.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except Contact.DoesNotExist:
            pass  # New contact, allow subscription

        # 1. Enforce member cap
        if getattr(product, "max_members", None) is not None:
            active_count = CustomerMembership.objects.filter(
                product=product, status__in=["active", "trialing"]
            ).count()
            if active_count >= product.max_members:
                return Response(
                    {"error": "plan_full", "message": "This plan is currently full."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # 2. Validate required custom fields
        custom_data = request.data.get("custom_data") or {}
        if not isinstance(custom_data, dict):
            custom_data = {}
        for field in (getattr(product, "signup_fields", None) or []):
            if field.get("required") and not custom_data.get(field.get("key")):
                return Response(
                    {
                        "error": "missing_field",
                        "message": f"{field.get('label', field.get('key', 'Field'))} is required.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        first_name = (request.data.get("first_name") or "").strip()
        last_name = (request.data.get("last_name") or "").strip()
        payment_method_id = (request.data.get("payment_method_id") or "").strip() or None

        # 3. Approval path: no Stripe, create pending_approval membership
        if getattr(product, "requires_approval", False):
            try:
                membership = create_approval_membership(
                    business=business,
                    product=product,
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    custom_data=custom_data,
                )
            except Exception as e:
                logger.exception("Widget membership subscribe (approval) failed: %s", e)
                return Response(
                    {"error": str(e)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {"status": "pending_approval", "membership_id": str(membership.id)},
                status=status.HTTP_200_OK,
            )

        # 4. Normal path: create Stripe subscription
        trial_period_days = getattr(product, "trial_period_days", None)
        try:
            membership, client_secret = create_customer_membership_subscription(
                business=business,
                product=product,
                email=email,
                first_name=first_name,
                last_name=last_name,
                payment_method_id=payment_method_id,
                custom_data=custom_data,
                trial_period_days=trial_period_days,
            )
        except Exception as e:
            logger.exception("Widget membership subscribe failed: %s", e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"client_secret": client_secret, "membership_id": str(membership.id)},
            status=status.HTTP_200_OK,
        )


class WidgetMembershipStatusView(APIView):
    """GET: Check if email has an active membership for this business."""

    permission_classes = [IsValidWidgetRequest]
    throttle_classes = [WidgetRateThrottle]

    def get(self, request):
        business = request.business_context
        email = (request.query_params.get("email") or "").strip()
        if not email:
            return Response(
                {"active": False, "error": "email is required"},
                status=status.HTTP_200_OK,
            )
        if getattr(business, "is_demo", False):
            return Response({"active": False}, status=status.HTTP_200_OK)
        try:
            contact = Contact.objects.get(
                business=business, email__iexact=email
            )
        except Contact.DoesNotExist:
            return Response({"active": False}, status=status.HTTP_200_OK)
        now = timezone.now()
        membership = (
            CustomerMembership.objects.filter(
                contact=contact,
                product__business=business,
                status__in=["active", "trialing"],
            )
            .filter(
                Q(current_period_end__isnull=True) | Q(current_period_end__gt=now)
            )
            .select_related("product")
            .first()
        )
        if not membership:
            return Response({"active": False}, status=status.HTTP_200_OK)
        product = membership.product
        credits_remaining = None
        if product.access_type == "credits" and product.credit_allowance:
            credits_remaining = get_credits_remaining(membership)
        return Response(
            {
                "active": True,
                "product_name": product.name,
                "credits_remaining": credits_remaining,
            },
            status=status.HTTP_200_OK,
        )
