"""
Recalculate platform fee and business net payout after a business-dashboard reschedule.

When the new session list price (× participants) is below the pre-tax amount the customer
paid, the difference is treated as additional platform commission; the business payout is
recomputed as if the booking were for the new session price (same fee rate as at checkout).
"""
from decimal import Decimal, ROUND_HALF_UP
import logging

from quickstart.utils.commission import get_plan_fee_percentage
from quickstart.utils.widget_booking_source import is_widget_booking_source
from quickstart.utils.stripe_processing_fee import estimate_stripe_processing_fee

logger = logging.getLogger(__name__)

HST_RATE = Decimal("0.13")


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _service_fee_rate(payment, business) -> Decimal:
    """Resolve commission rate from stored payment, else current plan fee."""
    meta = payment.metadata or {}
    orig = meta.get("original_stripe_metadata") or {}
    if not isinstance(orig, dict):
        orig = {}

    # Prefer rate implied by the stored payment + checkout subtotal (handles discounts).
    sub_meta = orig.get("subtotal_for_payout") or orig.get("subtotal_after_discount")
    if sub_meta is not None and payment.platform_fee_amount:
        try:
            sm = _q2(Decimal(str(sub_meta)))
            if sm > 0:
                implied = (payment.platform_fee_amount / sm).quantize(Decimal("0.0001"))
                if Decimal("0") <= implied <= Decimal("0.5"):
                    return implied
        except (ArithmeticError, ValueError, TypeError):
            pass

    plan_id = orig.get("plan_id") if is_widget_booking_source(orig.get("booking_source")) else None
    return get_plan_fee_percentage(business, plan_id=plan_id) / Decimal("100.0")


def preview_reschedule_payout_adjustment(booking, new_schedule_instance):
    """
    Compute post-reschedule payout split if the new session is cheaper (pre-tax list vs charged).

    Returns:
        None: caller should treat as "no preview" (e.g. missing payment).
        dict: always includes should_apply (bool) and optional reason / amounts.
    """
    if booking.enrollment_type != "Single Session":
        return {"should_apply": False, "reason": "not_single_session"}

    if booking.payout_status != "pending":
        return {"should_apply": False, "reason": "payout_not_pending"}

    if booking.payment_status != "paid":
        return {"should_apply": False, "reason": "not_paid"}

    payment = (
        booking.payments.filter(status="succeeded")
        .order_by("-created_at")
        .first()
    )
    if not payment:
        return {"should_apply": False, "reason": "no_succeeded_payment"}

    tax_amount = payment.tax_amount or Decimal("0.00")
    s_customer = _q2(payment.amount - tax_amount)
    if s_customer <= 0:
        return {"should_apply": False, "reason": "non_positive_customer_subtotal"}

    participants = booking.participants or 1
    s_new = _q2(Decimal(str(new_schedule_instance.price)) * participants)

    if s_new >= s_customer:
        return {
            "should_apply": False,
            "reason": "new_list_not_cheaper_than_charged_subtotal",
            "s_customer": s_customer,
            "s_new": s_new,
        }

    business = new_schedule_instance.schedule.option.classId.businessId
    rate = _service_fee_rate(payment, business)
    base_commission = _q2(s_new * rate)
    overage = _q2(s_customer - s_new)
    platform_fee_amount = _q2(overage + base_commission)
    platform_fee_tax = _q2(platform_fee_amount * HST_RATE)
    stripe_fee = payment.stripe_processing_fee or estimate_stripe_processing_fee(
        payment.amount
    )
    net_payout = _q2(
        payment.amount - platform_fee_amount - platform_fee_tax - stripe_fee
    )

    return {
        "should_apply": True,
        "payment": payment,
        "s_customer": s_customer,
        "s_new": s_new,
        "service_fee_rate": rate,
        "platform_fee_amount": platform_fee_amount,
        "platform_fee_tax": platform_fee_tax,
        "net_payout_amount": net_payout,
    }


def apply_reschedule_payout_adjustment(booking, new_schedule_instance):
    """
    Persist Payment + Booking allocated_net_payout when preview says should_apply.
    Returns the preview dict (with should_apply False) if skipped.
    """
    preview = preview_reschedule_payout_adjustment(booking, new_schedule_instance)
    if not preview.get("should_apply"):
        return preview

    payment = preview["payment"]
    payment.platform_fee_amount = preview["platform_fee_amount"]
    payment.platform_fee_tax = preview["platform_fee_tax"]
    payment.net_payout_amount = preview["net_payout_amount"]
    payment.save(
        update_fields=[
            "platform_fee_amount",
            "platform_fee_tax",
            "net_payout_amount",
        ]
    )

    booking.allocated_net_payout = preview["net_payout_amount"]
    booking.save(update_fields=["allocated_net_payout"])

    logger.info(
        "Reschedule payout adjustment booking=%s payment=%s: "
        "s_customer=%s s_new=%s platform_fee=%s net_payout=%s",
        booking.id,
        payment.id,
        preview["s_customer"],
        preview["s_new"],
        preview["platform_fee_amount"],
        preview["net_payout_amount"],
    )

    return preview
