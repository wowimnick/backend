"""
Widget subscription service: one row per business, create/upgrade/downgrade/same-price.
Used by WidgetSubscriptionView POST; GET reads the single row from DB only.
"""
import logging
from datetime import datetime

import pytz
import stripe
from django.conf import settings
from django.utils import timezone

from quickstart.models import BusinessInfo, WidgetSubscription
from quickstart.services.subscription_sync import (
    _widget_price_to_plan_id,
    sync_widget_subscription_from_stripe,
)

logger = logging.getLogger(__name__)

VALID_PLAN_IDS = {"basic", "growth", "advanced"}
PLAN_ORDER = ["basic", "growth", "advanced"]


def _stripe_attr(obj, key, default=None):
    """StripeObject has no dict-like .get(); use this for dict-or-Stripe reads."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def get_widget_subscription(business):
    """Return the single WidgetSubscription for the business, or None."""
    return getattr(business, "widget_subscription", None)


def _price_map():
    return {
        "basic": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC", None),
        "growth": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH", None),
        "advanced": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED", None),
    }


def _get_price_id(plan_id):
    return _price_map().get(plan_id) or getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ID", None)


def _is_downgrade(from_plan_id, to_plan_id):
    if from_plan_id not in VALID_PLAN_IDS or to_plan_id not in VALID_PLAN_IDS:
        return False
    return PLAN_ORDER.index(to_plan_id) < PLAN_ORDER.index(from_plan_id)


def _client_secret_from_stripe_invoice(invoice):
    """Extract payment_intent client_secret from a Stripe invoice (for PIs that need confirmation)."""
    if invoice is None or isinstance(invoice, str):
        return None
    inv_id = getattr(invoice, "id", None) or (invoice.get("id") if isinstance(invoice, dict) else None)
    pi = getattr(invoice, "payment_intent", None) or (invoice.get("payment_intent") if isinstance(invoice, dict) else None)
    if pi is None:
        payments = getattr(invoice, "payments", None) or (invoice.get("payments") if isinstance(invoice, dict) else None)
        if payments:
            data = getattr(payments, "data", None) or (
                payments.get("data") if isinstance(payments, dict) else None
            )
            if data and len(data):
                first = data[0]
                payment = getattr(first, "payment", None) or (first.get("payment") if isinstance(first, dict) else None)
                if payment:
                    pi = getattr(payment, "payment_intent", None) or (payment.get("payment_intent") if isinstance(payment, dict) else None)
    if pi is None:
        return None
    pi_id = pi if isinstance(pi, str) else (getattr(pi, "id", None) or (pi.get("id") if isinstance(pi, dict) else None))
    if not pi_id:
        return None
    try:
        pi_obj = stripe.PaymentIntent.retrieve(pi_id)
        pi_status = getattr(pi_obj, "status", None) or (pi_obj.get("status") if isinstance(pi_obj, dict) else None)
        if pi_status not in ("requires_payment_method", "requires_confirmation", "requires_action"):
            return None
        return getattr(pi_obj, "client_secret", None) or (pi_obj.get("client_secret") if isinstance(pi_obj, dict) else None)
    except stripe.StripeError:
        return None


def _get_default_payment_method_id(business):
    """Return the business's default payment method id for charging, or None."""
    if not business.stripe_customer_id:
        return None
    try:
        customer = stripe.Customer.retrieve(
            business.stripe_customer_id,
            expand=["invoice_settings.default_payment_method"],
        )
        default_pm = getattr(getattr(customer, "invoice_settings", None), "default_payment_method", None)
        if default_pm is not None:
            pm_id = default_pm if isinstance(default_pm, str) else (getattr(default_pm, "id", None) or (default_pm.get("id") if isinstance(default_pm, dict) else None))
            if pm_id:
                return pm_id
    except stripe.StripeError:
        pass
    sub = get_widget_subscription(business)
    if sub and sub.stripe_subscription_id:
        try:
            stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["default_payment_method"])
            default_pm = getattr(stripe_sub, "default_payment_method", None) or (stripe_sub.get("default_payment_method") if isinstance(stripe_sub, dict) else None)
            if default_pm is not None:
                pm_id = default_pm if isinstance(default_pm, str) else (getattr(default_pm, "id", None) or (default_pm.get("id") if isinstance(default_pm, dict) else None))
                if pm_id:
                    return pm_id
        except stripe.StripeError:
            pass
    return None


def create_subscription(business, plan_id):
    """
    Ensure Stripe customer; cancel any existing incomplete Stripe sub for this business;
    create new Stripe subscription; update or create the single WidgetSubscription row.
    Returns (stripe_sub, client_secret, widget_sub). client_secret may be None if invoice already paid.
    """
    price_id = _get_price_id(plan_id)
    if not price_id:
        raise ValueError("Widget subscription pricing is not configured.")

    if not business.stripe_customer_id:
        customer = stripe.Customer.create(
            email=business.studentContactEmail,
            name=business.businessName,
            metadata={"business_id": str(business.businessId)},
        )
        business.stripe_customer_id = customer.id
        business.save(update_fields=["stripe_customer_id"])

    row = get_widget_subscription(business)
    if row and row.stripe_subscription_id and (row.status or "").strip().lower() in ("incomplete", "incomplete_expired"):
        try:
            stripe.Subscription.cancel(row.stripe_subscription_id)
        except stripe.StripeError:
            pass

    pm_id = _get_default_payment_method_id(business)
    create_params = {
        "customer": business.stripe_customer_id,
        "items": [{"price": price_id}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        "expand": ["latest_invoice.payment_intent", "latest_invoice.payments"],
        "metadata": {"business_id": str(business.businessId), "plan_id": plan_id},
    }
    if pm_id:
        create_params["default_payment_method"] = pm_id

    stripe_sub = stripe.Subscription.create(**create_params)

    client_secret = _client_secret_from_stripe_invoice(
        getattr(stripe_sub, "latest_invoice", None)
    )
    if not client_secret and stripe_sub.id:
        try:
            expanded = stripe.Subscription.retrieve(stripe_sub.id, expand=["latest_invoice.payments"])
            inv = getattr(expanded, "latest_invoice", None)
            client_secret = _client_secret_from_stripe_invoice(inv)
        except stripe.StripeError:
            pass
    if not client_secret:
        latest_inv = getattr(stripe_sub, "latest_invoice", None)
        inv_id = getattr(latest_inv, "id", None) if latest_inv and not isinstance(latest_inv, str) else (latest_inv if isinstance(latest_inv, str) else None)
        if inv_id:
            try:
                inv_obj = stripe.Invoice.retrieve(str(inv_id), expand=["payments"])
                client_secret = _client_secret_from_stripe_invoice(inv_obj)
            except stripe.StripeError:
                pass

    stripe_price_id = None
    items_obj = _stripe_attr(stripe_sub, "items")
    data = _stripe_attr(items_obj, "data") if items_obj is not None else None
    if data:
        first = data[0]
        price = _stripe_attr(first, "price")
        if isinstance(price, str):
            stripe_price_id = price
        elif price is not None:
            stripe_price_id = _stripe_attr(price, "id")

    sub_status = _stripe_attr(stripe_sub, "status") or "incomplete"

    row, _ = WidgetSubscription.objects.get_or_create(
        business=business,
        defaults={
            "stripe_subscription_id": stripe_sub.id,
            "stripe_customer_id": business.stripe_customer_id or "",
            "stripe_price_id": stripe_price_id,
            "status": sub_status,
            "plan_id": plan_id,
        },
    )
    if row.stripe_subscription_id != stripe_sub.id:
        row.stripe_subscription_id = stripe_sub.id
        row.stripe_customer_id = business.stripe_customer_id or ""
        row.stripe_price_id = stripe_price_id
        row.status = sub_status
        row.save(update_fields=["stripe_subscription_id", "stripe_customer_id", "stripe_price_id", "status"])

    return stripe_sub, client_secret, row


def same_price_open_invoice(sub, plan_id, price_id):
    """
    Stripe already has this price. If there's an open invoice with a usable PI, return (True, client_secret, sub).
    Else sync DB and return (False, None, synced_sub) for success.
    Returns (requires_payment: bool or None, client_secret: str or None, sub_for_response).
    None for requires_payment means error (open invoice but no client_secret).
    """
    try:
        open_list = stripe.Invoice.list(subscription=sub.stripe_subscription_id, status="open", limit=1)
        open_data = getattr(open_list, "data", None) or []
    except stripe.StripeError as e:
        logger.warning("widget_subscription_service: open invoice check failed: %s", e)
        return False, None, sub
    if open_data:
        client_secret = _client_secret_from_stripe_invoice(open_data[0])
        if client_secret:
            return True, client_secret, sub
        return None, None, sub
    synced, _ = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
    return False, None, synced or sub


def downgrade_subscription(sub, plan_id, business):
    """
    Schedule downgrade at period end via Stripe Subscription Schedule.
    Returns (synced_sub, error_message). error_message is None on success.
    If the subscription already has a schedule (e.g. from cancel_at_period_end), we update it
    instead of creating a new one (Stripe does not allow creating a schedule for a sub that has one).
    """
    price_id = _get_price_id(plan_id)
    if not price_id:
        return None, "Widget subscription pricing is not configured."
    try:
        stripe_sub = stripe.Subscription.retrieve(
            sub.stripe_subscription_id,
            expand=["items.data.price", "schedule"],
        )
    except stripe.StripeError as e:
        logger.warning("widget_subscription_service: Stripe retrieve failed: %s", e)
        return None, str(e)
    items_data = _stripe_attr(stripe_sub, "items") or {}
    item_list = _stripe_attr(items_data, "data") or []
    if not item_list:
        return None, "Invalid subscription state."
    first_item = item_list[0] if item_list else None
    price_obj = _stripe_attr(first_item, "price") if first_item else None
    if isinstance(price_obj, str):
        current_price_id = price_obj
    elif price_obj is not None:
        current_price_id = _stripe_attr(price_obj, "id")
    else:
        current_price_id = None
    # Stripe API: current_period_end is on each Subscription Item, not on the Subscription object
    period_end_ts = (
        getattr(first_item, "current_period_end", None)
        if first_item
        else None
    ) or getattr(stripe_sub, "current_period_end", None)
    if not period_end_ts:
        logger.warning(
            "widget_subscription_service: downgrade sub_id=%s missing current_period_end on sub and first item",
            sub.stripe_subscription_id,
        )
        return None, "Could not schedule downgrade. Please try again."

    def _first_phase_start(phases_list):
        """Get start_date of current (first) phase. Stripe forbids modifying current phase start_date."""
        if not phases_list:
            return None
        p0 = phases_list[0]
        return p0.get("start_date") if isinstance(p0, dict) else getattr(p0, "start_date", None)

    metadata_payload = {"business_id": str(business.businessId), "plan_id": plan_id}

    existing_schedule = _stripe_attr(stripe_sub, "schedule")
    schedule_id = (
        existing_schedule
        if isinstance(existing_schedule, str)
        else (getattr(existing_schedule, "id", None) if existing_schedule else None)
    )
    logger.info(
        "widget_subscription_service: downgrade sub_id=%s plan_id=%s schedule_id=%s period_end_ts=%s",
        sub.stripe_subscription_id,
        plan_id,
        schedule_id,
        period_end_ts,
    )
    if schedule_id:
        try:
            schedule = stripe.SubscriptionSchedule.retrieve(schedule_id, expand=["phases"])
            phases = getattr(schedule, "phases", None) or _stripe_attr(schedule, "phases") or []
            logger.info(
                "widget_subscription_service: downgrade existing schedule id=%s phases_count=%s",
                schedule_id,
                len(phases),
            )
            first_start = _first_phase_start(phases)
            if not first_start:
                return None, "Could not schedule downgrade. Please try again."
            # Same plan as current = cancel scheduled downgrade (release schedule).
            if price_id == current_price_id:
                stripe.SubscriptionSchedule.release(schedule_id)
                logger.info(
                    "widget_subscription_service: downgrade cancelled (schedule released) schedule_id=%s",
                    schedule_id,
                )
            else:
                # One or two phases: set/update scheduled change to target plan at period end.
                phases_payload = [
                    {
                        "items": [{"price": current_price_id}],
                        "start_date": first_start,
                        "end_date": period_end_ts,
                    },
                    {"items": [{"price": price_id}], "proration_behavior": "none"},
                ]
                stripe.SubscriptionSchedule.modify(
                    schedule_id,
                    phases=phases_payload,
                    metadata=metadata_payload,
                )
                logger.info(
                    "widget_subscription_service: downgrade schedule updated schedule_id=%s",
                    schedule_id,
                )
        except stripe.StripeError as e:
            err_msg = str(e)
            err_code = getattr(e, "code", None)
            err_type = type(e).__name__
            logger.warning(
                "widget_subscription_service: SubscriptionSchedule update failed type=%s code=%s msg=%s full=%r",
                err_type,
                err_code,
                err_msg,
                e,
            )
            return None, err_msg if err_msg else "Could not schedule downgrade. Please try again."
    else:
        try:
            logger.info(
                "widget_subscription_service: downgrade creating new schedule for sub_id=%s",
                sub.stripe_subscription_id,
            )
            schedule = stripe.SubscriptionSchedule.create(
                from_subscription=sub.stripe_subscription_id,
                expand=["phases"],
            )
            created_phases = getattr(schedule, "phases", None) or _stripe_attr(schedule, "phases") or []
            first_start = _first_phase_start(created_phases)
            if not first_start:
                return None, "Could not schedule downgrade. Please try again."
            phases_payload = [
                {
                    "items": [{"price": current_price_id}],
                    "start_date": first_start,
                    "end_date": period_end_ts,
                },
                {"items": [{"price": price_id}], "proration_behavior": "none"},
            ]
            stripe.SubscriptionSchedule.modify(
                schedule.id,
                phases=phases_payload,
                metadata=metadata_payload,
            )
            logger.info(
                "widget_subscription_service: downgrade schedule created and updated schedule_id=%s",
                getattr(schedule, "id", None),
            )
        except stripe.StripeError as e:
            err_msg = str(e)
            err_code = getattr(e, "code", None)
            err_type = type(e).__name__
            logger.warning(
                "widget_subscription_service: SubscriptionSchedule create/update failed type=%s code=%s msg=%s full=%r",
                err_type,
                err_code,
                err_msg,
                e,
            )
            return None, err_msg if err_msg else "Could not schedule downgrade. Please try again."
    synced, _ = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
    return synced or sub, None


def cancel_at_period_end(sub):
    """
    Set subscription to cancel at period end. When a schedule is attached, Stripe requires
    updating the schedule (single phase to period end with end_behavior=cancel) instead of
    Subscription.modify(cancel_at_period_end=True). Returns (synced_sub or sub, error_msg).
    """
    try:
        stripe_sub = stripe.Subscription.retrieve(
            sub.stripe_subscription_id,
            expand=["items.data.price", "schedule"],
        )
    except stripe.StripeError as e:
        logger.warning("widget_subscription_service: cancel retrieve failed: %s", e)
        return None, str(e)
    schedule_ref = _stripe_attr(stripe_sub, "schedule")
    schedule_id = (
        schedule_ref
        if isinstance(schedule_ref, str)
        else (getattr(schedule_ref, "id", None) if schedule_ref else None)
    )
    if not schedule_id:
        try:
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
        except stripe.StripeError as e:
            logger.warning("widget_subscription_service: cancel modify failed: %s", e)
            return None, str(e)
        synced, _ = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
        return synced or sub, None
    # Subscription has a schedule: update schedule to single phase ending at period end with end_behavior=cancel.
    items_data = _stripe_attr(stripe_sub, "items") or {}
    item_list = _stripe_attr(items_data, "data") or []
    if not item_list:
        return None, "Invalid subscription state."
    first_item = item_list[0]
    price_obj = _stripe_attr(first_item, "price")
    if isinstance(price_obj, str):
        current_price_id = price_obj
    elif price_obj is not None:
        current_price_id = _stripe_attr(price_obj, "id")
    else:
        current_price_id = None
    period_end_ts = getattr(first_item, "current_period_end", None) or getattr(
        stripe_sub, "current_period_end", None
    )
    if not current_price_id or not period_end_ts:
        return None, "Could not schedule cancellation. Please try again."
    try:
        schedule = stripe.SubscriptionSchedule.retrieve(schedule_id, expand=["phases"])
        phases = getattr(schedule, "phases", None) or _stripe_attr(schedule, "phases") or []
        first_start = (
            _stripe_attr(phases[0], "start_date") if phases else None
        )
        if not first_start:
            return None, "Could not schedule cancellation. Please try again."
        stripe.SubscriptionSchedule.modify(
            schedule_id,
            phases=[
                {
                    "items": [{"price": current_price_id}],
                    "start_date": first_start,
                    "end_date": period_end_ts,
                },
            ],
            end_behavior="cancel",
        )
    except stripe.StripeError as e:
        logger.warning("widget_subscription_service: cancel schedule update failed: %s", e)
        return None, str(e)
    synced, _ = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
    return synced or sub, None


def reactivate(sub):
    """
    Clear cancel_at_period_end. When a schedule is attached, release the schedule instead of
    Subscription.modify(cancel_at_period_end=False). Returns (synced_sub or sub, error_msg).
    """
    try:
        stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["schedule"])
    except stripe.StripeError as e:
        logger.warning("widget_subscription_service: reactivate retrieve failed: %s", e)
        return None, str(e)
    schedule_ref = _stripe_attr(stripe_sub, "schedule")
    schedule_id = (
        schedule_ref
        if isinstance(schedule_ref, str)
        else (getattr(schedule_ref, "id", None) if schedule_ref else None)
    )
    if not schedule_id:
        try:
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=False)
        except stripe.StripeError as e:
            logger.warning("widget_subscription_service: reactivate modify failed: %s", e)
            return None, str(e)
    else:
        try:
            stripe.SubscriptionSchedule.release(schedule_id)
        except stripe.StripeError as e:
            logger.warning("widget_subscription_service: reactivate release failed: %s", e)
            return None, str(e)
    synced, _ = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
    return synced or sub, None


def upgrade_subscription(sub, plan_id, business):
    """
    Modify Stripe subscription to new price (proration); use default_payment_method when available.
    Returns (requires_payment: bool, client_secret: str or None, updated_sub or None, error: str or None).
    On success with no payment required: requires_payment=False, client_secret=None, updated_sub set, error=None.
    On success with payment required: requires_payment=True, client_secret set, updated_sub=sub, error=None.
    On failure: error set.
    """
    price_id = _get_price_id(plan_id)
    if not price_id:
        return None, None, None, "Widget subscription pricing is not configured."
    try:
        stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["items.data.price"])
    except stripe.StripeError as e:
        logger.warning("widget_subscription_service: Stripe retrieve failed: %s", e)
        return None, None, None, str(e)
    items_data = _stripe_attr(stripe_sub, "items") or {}
    item_list = _stripe_attr(items_data, "data") or []
    if not item_list:
        return None, None, None, "Invalid subscription state."
    subscription_item_id = _stripe_attr(item_list[0], "id")
    if not subscription_item_id:
        return None, None, None, "Invalid subscription state."

    # Stripe does not allow default_payment_method when payment_behavior is pending_if_incomplete.
    # The pending invoice will use the customer's invoice_settings.default_payment_method if set;
    # otherwise we return client_secret so the frontend can collect payment.
    modify_params = {
        "items": [{"id": subscription_item_id, "price": price_id}],
        "proration_behavior": "always_invoice",
        "payment_behavior": "pending_if_incomplete",
        "expand": ["latest_invoice", "latest_invoice.payment_intent", "latest_invoice.payments"],
    }
    try:
        stripe_sub = stripe.Subscription.modify(sub.stripe_subscription_id, **modify_params)
    except stripe.StripeError as e:
        logger.warning("widget_subscription_service: Subscription.modify failed: %s", e)
        return None, None, None, str(e)
    try:
        stripe.Subscription.modify(
            sub.stripe_subscription_id,
            metadata={"business_id": str(business.businessId), "plan_id": plan_id},
            cancel_at_period_end=False,
        )
    except stripe.StripeError:
        pass

    latest_invoice = getattr(stripe_sub, "latest_invoice", None)
    latest_inv_id = None
    latest_inv_status = None
    if latest_invoice:
        if isinstance(latest_invoice, str):
            latest_inv_id = latest_invoice
        else:
            latest_inv_id = getattr(latest_invoice, "id", None) or (latest_invoice.get("id") if isinstance(latest_invoice, dict) else None)
            latest_inv_status = getattr(latest_invoice, "status", None) or (latest_invoice.get("status") if isinstance(latest_invoice, dict) else None)

    client_secret = _client_secret_from_stripe_invoice(latest_invoice) if latest_invoice and not isinstance(latest_invoice, str) else None
    if not client_secret and latest_inv_id and isinstance(latest_invoice, str):
        try:
            inv_obj = stripe.Invoice.retrieve(latest_inv_id, expand=["payment_intent", "payments"])
            client_secret = _client_secret_from_stripe_invoice(inv_obj)
        except stripe.StripeError:
            pass
    if client_secret:
        return True, client_secret, sub, None

    if not client_secret and sub.stripe_subscription_id:
        try:
            open_invoices = stripe.Invoice.list(subscription=sub.stripe_subscription_id, status="open", limit=1)
            open_data = getattr(open_invoices, "data", None) or []
            if open_data:
                client_secret = _client_secret_from_stripe_invoice(open_data[0])
                if client_secret:
                    return True, client_secret, sub, None
        except stripe.StripeError:
            pass

    if latest_inv_status == "open":
        return None, None, None, "This payment link is no longer valid. Please try switching plan again."
    try:
        open_check = stripe.Invoice.list(subscription=sub.stripe_subscription_id, status="open", limit=1)
        if getattr(open_check, "data", None):
            return None, None, None, "Payment is required to complete this plan change. Please complete payment when prompted or try again."
    except stripe.StripeError:
        pass

    try:
        stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["items.data.price"])
    except stripe.StripeError:
        pass
    items_wrap = _stripe_attr(stripe_sub, "items") or {}
    stripe_items = _stripe_attr(items_wrap, "data") or []
    stripe_price_id_now = None
    if stripe_items:
        pr = _stripe_attr(stripe_items[0], "price")
        if pr:
            stripe_price_id_now = pr if isinstance(pr, str) else _stripe_attr(pr, "id")
    if stripe_price_id_now != price_id:
        return None, None, None, "Payment is required to complete this plan change. Please try again and complete payment when prompted."

    period_end = getattr(stripe_sub, "current_period_end", None)
    current_period_end = datetime.fromtimestamp(period_end, tz=pytz.UTC) if period_end else None
    now = timezone.now()
    if current_period_end is not None and current_period_end <= now:
        current_period_end = sub.current_period_end
    sub.plan_id = plan_id
    sub.stripe_price_id = price_id
    if current_period_end is not None:
        sub.current_period_end = current_period_end
    sub.status = _stripe_attr(stripe_sub, "status") or sub.status
    sub.cancel_at_period_end = bool(getattr(stripe_sub, "cancel_at_period_end", None))
    sub.save(update_fields=["plan_id", "stripe_price_id", "current_period_end", "status", "cancel_at_period_end"])
    return False, None, sub, None
