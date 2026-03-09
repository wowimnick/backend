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
            data = getattr(payments, "data", None) or payments.get("data")
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
        getattr(stripe_sub, "latest_invoice", None) or stripe_sub.get("latest_invoice")
    )
    if not client_secret and stripe_sub.id:
        try:
            expanded = stripe.Subscription.retrieve(stripe_sub.id, expand=["latest_invoice.payments"])
            inv = getattr(expanded, "latest_invoice", None) or expanded.get("latest_invoice")
            client_secret = _client_secret_from_stripe_invoice(inv)
        except stripe.StripeError:
            pass
    if not client_secret:
        latest_inv = getattr(stripe_sub, "latest_invoice", None) or stripe_sub.get("latest_invoice")
        inv_id = getattr(latest_inv, "id", None) if latest_inv and not isinstance(latest_inv, str) else (latest_inv if isinstance(latest_inv, str) else None)
        if inv_id:
            try:
                inv_obj = stripe.Invoice.retrieve(str(inv_id), expand=["payments"])
                client_secret = _client_secret_from_stripe_invoice(inv_obj)
            except stripe.StripeError:
                pass

    stripe_price_id = None
    if stripe_sub.get("items") and stripe_sub["items"].get("data"):
        stripe_price_id = stripe_sub["items"]["data"][0].get("price", {}).get("id")

    row, _ = WidgetSubscription.objects.get_or_create(
        business=business,
        defaults={
            "stripe_subscription_id": stripe_sub.id,
            "stripe_customer_id": business.stripe_customer_id or "",
            "stripe_price_id": stripe_price_id,
            "status": stripe_sub.get("status", "incomplete"),
            "plan_id": plan_id,
        },
    )
    if row.stripe_subscription_id != stripe_sub.id:
        row.stripe_subscription_id = stripe_sub.id
        row.stripe_customer_id = business.stripe_customer_id or ""
        row.stripe_price_id = stripe_price_id
        row.status = stripe_sub.get("status", "incomplete")
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
        open_data = open_list.get("data") or []
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
    items_data = stripe_sub.get("items") or {}
    item_list = (items_data.get("data") or []) if isinstance(items_data, dict) else []
    if not item_list:
        return None, "Invalid subscription state."
    current_price_id = (item_list[0].get("price") or {}).get("id") if item_list else None
    period_end_ts = getattr(stripe_sub, "current_period_end", None) or stripe_sub.get("current_period_end")
    if not period_end_ts:
        logger.warning(
            "widget_subscription_service: downgrade sub_id=%s missing current_period_end (sub keys: %s)",
            sub.stripe_subscription_id,
            list(stripe_sub.keys()) if hasattr(stripe_sub, "keys") else getattr(stripe_sub, "__dict__", {}),
        )
        return None, "Could not schedule downgrade. Please try again."
    phases_payload = [
        {"items": [{"price": current_price_id}], "end_date": period_end_ts},
        {"items": [{"price": price_id}], "proration_behavior": "none"},
    ]
    metadata_payload = {"business_id": str(business.businessId), "plan_id": plan_id}

    existing_schedule = stripe_sub.get("schedule")
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
            phases = getattr(schedule, "phases", None) or schedule.get("phases") or []
            logger.info(
                "widget_subscription_service: downgrade existing schedule id=%s phases_count=%s",
                schedule_id,
                len(phases),
            )
            if len(phases) >= 2:
                return None, "A plan change is already scheduled. It will take effect at the end of your billing period."
            # Subscription already has a schedule (e.g. from cancel_at_period_end). Update it.
            stripe.SubscriptionSchedule.update(
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
                from_subscription=sub.stripe_subscription_id
            )
            stripe.SubscriptionSchedule.update(
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
    items_data = stripe_sub.get("items") or {}
    item_list = (items_data.get("data") or []) if isinstance(items_data, dict) else []
    if not item_list:
        return None, None, None, "Invalid subscription state."
    subscription_item_id = item_list[0].get("id")
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

    latest_invoice = getattr(stripe_sub, "latest_invoice", None) or stripe_sub.get("latest_invoice")
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
            open_data = open_invoices.get("data") or []
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
        if open_check.get("data") or []:
            return None, None, None, "Payment is required to complete this plan change. Please complete payment when prompted or try again."
    except stripe.StripeError:
        pass

    try:
        stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id, expand=["items.data.price"])
    except stripe.StripeError:
        pass
    stripe_items = (stripe_sub.get("items") or {}).get("data") or []
    stripe_price_id_now = None
    if stripe_items and stripe_items[0].get("price"):
        stripe_price_id_now = stripe_items[0]["price"].get("id") if isinstance(stripe_items[0]["price"], dict) else getattr(stripe_items[0]["price"], "id", None)
    if stripe_price_id_now != price_id:
        return None, None, None, "Payment is required to complete this plan change. Please try again and complete payment when prompted."

    period_end = stripe_sub.get("current_period_end")
    current_period_end = datetime.fromtimestamp(period_end, tz=pytz.UTC) if period_end else None
    now = timezone.now()
    if current_period_end is not None and current_period_end <= now:
        current_period_end = sub.current_period_end
    sub.plan_id = plan_id
    sub.stripe_price_id = price_id
    if current_period_end is not None:
        sub.current_period_end = current_period_end
    sub.status = stripe_sub.get("status") or sub.status
    sub.cancel_at_period_end = bool(stripe_sub.get("cancel_at_period_end"))
    sub.save(update_fields=["plan_id", "stripe_price_id", "current_period_end", "status", "cancel_at_period_end"])
    return False, None, sub, None
