"""
Customer membership subscription service: Stripe Product/Price for MembershipProduct,
create customer subscription, credit ledger helpers.
"""
import logging
from datetime import datetime
from decimal import Decimal

import stripe
from django.conf import settings
from django.utils import timezone

from django.db.models import Sum

from quickstart.models import (
    Contact,
    CustomerMembership,
    MembershipCreditLedger,
    MembershipProduct,
)
from quickstart.utils.commission import get_plan_fee_percentage
from quickstart.utils.email_utils import (
    send_membership_lifecycle_member_email,
    send_membership_lifecycle_business_email,
)

logger = logging.getLogger(__name__)

_CONNECT_MISSING_ERROR = (
    "Payouts must be connected before memberships can be charged. "
    "Connect Stripe in your dashboard first."
)


def _require_connect_for_membership_charges(business):
    if not getattr(business, "stripe_account_id", None):
        raise ValueError(_CONNECT_MISSING_ERROR)
    if getattr(business, "stripe_account_status", None) != "active":
        raise ValueError(_CONNECT_MISSING_ERROR)


def _apply_membership_destination_charge(create_params, business):
    _require_connect_for_membership_charges(business)
    create_params["transfer_data"] = {"destination": business.stripe_account_id}
    create_params["application_fee_percent"] = float(get_plan_fee_percentage(business))


def _membership_resolve_stripe_customer_id(business, contact, email, first_name, last_name):
    """Reuse Contact or prior memberships' Stripe customer when possible."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    cid = (contact.stripe_customer_id or "").strip() or None
    if cid:
        try:
            stripe.Customer.retrieve(cid)
            return cid
        except stripe.StripeError:
            pass
    prior = (
        CustomerMembership.objects.filter(contact=contact)
        .exclude(stripe_customer_id__isnull=True)
        .exclude(stripe_customer_id="")
        .order_by("-updated_at")
        .first()
    )
    if prior and prior.stripe_customer_id:
        try:
            stripe.Customer.retrieve(prior.stripe_customer_id)
            cid = prior.stripe_customer_id
            contact.stripe_customer_id = cid
            contact.save(update_fields=["stripe_customer_id"])
            return cid
        except stripe.StripeError:
            pass
    try:
        existing = stripe.Customer.list(email=email, limit=10)
        for c in getattr(existing, "data", []) or []:
            md = getattr(c, "metadata", None) or {}
            if isinstance(md, dict):
                bid = md.get("business_id")
            else:
                bid = getattr(md, "business_id", None)
            if str(bid or "") == str(business.businessId):
                cid = c.id
                contact.stripe_customer_id = cid
                contact.save(update_fields=["stripe_customer_id"])
                return cid
    except stripe.StripeError as e:
        logger.warning("membership_service: Customer.list failed: %s", e)

    display_name = f"{first_name or ''} {last_name or ''}".strip() or email
    cust = stripe.Customer.create(
        email=email,
        name=display_name,
        metadata={
            "business_id": str(business.businessId),
            "contact_id": str(contact.id),
        },
    )
    contact.stripe_customer_id = cust.id
    contact.save(update_fields=["stripe_customer_id"])
    return cust.id


def create_stripe_price(product):
    """
    Create or ensure Stripe Product + Price for this MembershipProduct.
    Idempotent: if product.stripe_price_id is set, returns it without creating again.
    Returns the Stripe Price id (product.stripe_price_id after save).
    """
    if product.stripe_price_id:
        try:
            stripe.Price.retrieve(product.stripe_price_id)
            return product.stripe_price_id
        except stripe.StripeError:
            pass

    stripe.api_key = settings.STRIPE_SECRET_KEY
    interval = product.billing_interval if product.billing_interval in ("month", "year") else "month"
    # Stripe unit_amount is in smallest currency unit (cents for CAD/USD)
    unit_amount = int(product.price * 100)

    # Create Stripe Product for this membership (one product per MembershipProduct)
    stripe_product = stripe.Product.create(
        name=product.name,
        description=(product.description or "")[:500] or None,
        metadata={"membership_product_id": str(product.id), "business_id": str(product.business_id)},
    )

    # Create recurring Price
    stripe_price = stripe.Price.create(
        product=stripe_product.id,
        unit_amount=unit_amount,
        currency=(product.currency or "cad").lower(),
        recurring={"interval": interval},
        metadata={"membership_product_id": str(product.id)},
    )

    product.stripe_price_id = stripe_price.id
    product.save(update_fields=["stripe_price_id"])
    return product.stripe_price_id


def create_customer_membership_subscription(
    business,
    product,
    email,
    first_name,
    last_name,
    payment_method_id=None,
    custom_data=None,
    trial_period_days=None,
):
    """
    Create Stripe Customer + Subscription for a customer membership.
    Creates or gets Contact, creates CustomerMembership with status=incomplete,
    creates Stripe Subscription in default_incomplete mode.
    Returns (customer_membership, client_secret). client_secret may be None if invoice already paid.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    price_id = product.stripe_price_id
    if not price_id:
        price_id = create_stripe_price(product)

    # Get or create Contact for this business + email
    contact, _ = Contact.objects.get_or_create(
        business=business,
        email__iexact=email,
        defaults={
            "first_name": first_name or "",
            "last_name": last_name or "",
            "email": email,
            "source": "widget_booking",
        },
    )
    if contact.first_name != (first_name or "") or contact.last_name != (last_name or ""):
        contact.first_name = first_name or contact.first_name
        contact.last_name = last_name or contact.last_name
        contact.save(update_fields=["first_name", "last_name"])

    stripe_customer_id = _membership_resolve_stripe_customer_id(
        business, contact, email, first_name, last_name
    )

    create_params = {
        "customer": stripe_customer_id,
        "items": [{"price": price_id}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        "expand": ["latest_invoice.payment_intent", "latest_invoice.payments"],
        "metadata": {
            "business_id": str(business.businessId),
            "membership_product_id": str(product.id),
            "contact_id": str(contact.id),
        },
    }
    if payment_method_id:
        create_params["default_payment_method"] = payment_method_id
    if trial_period_days is not None and trial_period_days > 0:
        create_params["trial_period_days"] = trial_period_days

    _apply_membership_destination_charge(create_params, business)
    stripe_sub = stripe.Subscription.create(**create_params)

    # Persist CustomerMembership so webhook can update it
    period_start = None
    period_end = None
    if getattr(stripe_sub, "current_period_start", None):
        period_start = timezone.make_aware(
            datetime.utcfromtimestamp(stripe_sub.current_period_start)
        )
    if getattr(stripe_sub, "current_period_end", None):
        period_end = timezone.make_aware(
            datetime.utcfromtimestamp(stripe_sub.current_period_end)
        )

    customer_membership = CustomerMembership.objects.create(
        product=product,
        contact=contact,
        user=None,
        stripe_subscription_id=stripe_sub.id,
        stripe_customer_id=stripe_customer_id,
        status="incomplete",
        current_period_start=period_start,
        current_period_end=period_end,
        source="widget",
        custom_data=custom_data or {},
    )

    # Extract client_secret using the same battle-tested pattern as widget_config_views.py.
    # Stripe's API shape varies by account API version:
    #   - Classic: invoice.payment_intent (expanded object or id string)
    #   - New (2024+): invoice.payments.data[0].payment.payment_intent
    # We try three passes: create response → re-retrieve sub → retrieve invoice directly.
    invoice = getattr(stripe_sub, "latest_invoice", None)
    client_secret = _client_secret_from_stripe_invoice(invoice)

    if not client_secret and stripe_sub.id:
        try:
            stripe_sub_expanded = stripe.Subscription.retrieve(
                stripe_sub.id,
                expand=["latest_invoice.payments"],
            )
            inv = getattr(stripe_sub_expanded, "latest_invoice", None)
            client_secret = _client_secret_from_stripe_invoice(inv)
        except stripe.StripeError as e:
            logger.warning("membership_service: Subscription.retrieve expand failed: %s", e)

    if not client_secret:
        latest_inv = getattr(stripe_sub, "latest_invoice", None)
        inv_id = (
            getattr(latest_inv, "id", None)
            if latest_inv is not None and not isinstance(latest_inv, str)
            else (latest_inv if isinstance(latest_inv, str) else None)
        )
        if inv_id:
            try:
                inv_obj = stripe.Invoice.retrieve(str(inv_id), expand=["payments"])
                client_secret = _client_secret_from_stripe_invoice(inv_obj)
            except stripe.StripeError as e:
                logger.warning("membership_service: Invoice.retrieve fallback failed: %s", e)

    return customer_membership, client_secret


def create_approval_membership(
    business, product, email, first_name, last_name, custom_data=None
):
    """
    Create a CustomerMembership with status=pending_approval (no Stripe).
    Used when product.requires_approval=True. Returns the membership.
    """
    contact, _ = Contact.objects.get_or_create(
        business=business,
        email__iexact=email,
        defaults={
            "first_name": first_name or "",
            "last_name": last_name or "",
            "email": email,
            "source": "widget_booking",
        },
    )
    if contact.first_name != (first_name or "") or contact.last_name != (last_name or ""):
        contact.first_name = first_name or contact.first_name
        contact.last_name = last_name or contact.last_name
        contact.save(update_fields=["first_name", "last_name"])

    membership = CustomerMembership.objects.create(
        product=product,
        contact=contact,
        user=None,
        stripe_subscription_id=None,
        stripe_customer_id=None,
        status="pending_approval",
        current_period_start=None,
        current_period_end=None,
        source="widget",
        custom_data=custom_data or {},
    )
    return membership


def approve_membership(membership):
    """
    For a membership with status=pending_approval: create Stripe Customer + Subscription
    (incomplete), save IDs on membership, set status=approved_pending_payment,
    send email to contact with hosted_invoice_url for payment.
    Returns (membership, hosted_invoice_url).
    """
    if membership.status != "pending_approval":
        raise ValueError("Membership must have status pending_approval to approve")
    if not membership.contact or not membership.contact.email:
        raise ValueError("Membership must have a contact with email")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    product = membership.product
    contact = membership.contact
    email = contact.email
    first_name = contact.first_name or ""
    last_name = contact.last_name or ""

    price_id = product.stripe_price_id
    if not price_id:
        price_id = create_stripe_price(product)

    stripe_customer = stripe.Customer.create(
        email=email,
        name=f"{first_name} {last_name}".strip() or email,
        metadata={
            "business_id": str(product.business_id),
            "membership_product_id": str(product.id),
            "contact_id": str(contact.id),
        },
    )
    stripe_customer_id = stripe_customer.id

    create_params = {
        "customer": stripe_customer_id,
        "items": [{"price": price_id}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        "expand": ["latest_invoice"],
        "metadata": {
            "business_id": str(product.business_id),
            "membership_product_id": str(product.id),
            "contact_id": str(contact.id),
        },
    }
    if getattr(product, "trial_period_days", None) and product.trial_period_days > 0:
        create_params["trial_period_days"] = product.trial_period_days

    _apply_membership_destination_charge(create_params, product.business)
    stripe_sub = stripe.Subscription.create(**create_params)

    period_start = None
    period_end = None
    if getattr(stripe_sub, "current_period_start", None):
        period_start = timezone.make_aware(
            datetime.utcfromtimestamp(stripe_sub.current_period_start)
        )
    if getattr(stripe_sub, "current_period_end", None):
        period_end = timezone.make_aware(
            datetime.utcfromtimestamp(stripe_sub.current_period_end)
        )

    membership.stripe_subscription_id = stripe_sub.id
    membership.stripe_customer_id = stripe_customer_id
    membership.status = "approved_pending_payment"
    membership.current_period_start = period_start
    membership.current_period_end = period_end
    membership.save(update_fields=[
        "stripe_subscription_id", "stripe_customer_id", "status",
        "current_period_start", "current_period_end", "updated_at",
    ])

    invoice = getattr(stripe_sub, "latest_invoice", None)
    hosted_invoice_url = None
    if invoice and not isinstance(invoice, str):
        hosted_invoice_url = getattr(invoice, "hosted_invoice_url", None) or (
            invoice.get("hosted_invoice_url") if isinstance(invoice, dict) else None
        )
    if not hosted_invoice_url and stripe_sub.id:
        try:
            inv_id = getattr(invoice, "id", None) if invoice and not isinstance(invoice, str) else None
            if inv_id:
                inv_obj = stripe.Invoice.retrieve(str(inv_id))
                hosted_invoice_url = getattr(inv_obj, "hosted_invoice_url", None) or (
                    inv_obj.get("hosted_invoice_url") if isinstance(inv_obj, dict) else None
                )
        except stripe.StripeError as e:
            logger.warning("approve_membership: could not get hosted_invoice_url: %s", e)

    try:
        send_membership_lifecycle_member_email(
            membership,
            lifecycle_event="approval_payment_required",
            payment_url=hosted_invoice_url,
            extra_message=(
                "Your application was approved. Please complete payment using the link below to activate your membership."
            ),
        )
        send_membership_lifecycle_business_email(
            membership,
            lifecycle_event="approval_payment_required",
            extra_message=(
                "This member was approved and is waiting to complete first payment."
            ),
        )
    except Exception as e:
        logger.exception("approve_membership: failed to send email: %s", e)
        # Still return success; business can resend link from dashboard if needed

    return membership, hosted_invoice_url


def _client_secret_from_stripe_invoice(invoice):
    """
    Extract payment_intent client_secret from a Stripe invoice object.
    Mirrors widget_config_views._client_secret_from_stripe_invoice exactly.
    Handles both classic (invoice.payment_intent) and new (invoice.payments) API shapes.
    """
    if invoice is None or isinstance(invoice, str):
        return None

    inv_id = getattr(invoice, "id", None) or (invoice.get("id") if isinstance(invoice, dict) else None)

    # Classic path: invoice.payment_intent
    pi = getattr(invoice, "payment_intent", None) or (
        invoice.get("payment_intent") if isinstance(invoice, dict) else None
    )

    # New API path: invoice.payments.data[0].payment.payment_intent
    if pi is None:
        payments = getattr(invoice, "payments", None) or (
            invoice.get("payments") if isinstance(invoice, dict) else None
        )
        if payments:
            data = getattr(payments, "data", None) or (payments.get("data") if isinstance(payments, dict) else None)
            if data and len(data):
                first = data[0]
                payment = getattr(first, "payment", None) or (
                    first.get("payment") if isinstance(first, dict) else None
                )
                if payment:
                    pi = getattr(payment, "payment_intent", None) or (
                        payment.get("payment_intent") if isinstance(payment, dict) else None
                    )

    if pi is None:
        logger.info("membership_service: no payment_intent found on invoice inv_id=%s", inv_id)
        return None

    pi_id = (
        pi if isinstance(pi, str)
        else (getattr(pi, "id", None) or (pi.get("id") if isinstance(pi, dict) else None))
    )
    if not pi_id:
        return None

    try:
        pi_obj = stripe.PaymentIntent.retrieve(pi_id)
        pi_status = getattr(pi_obj, "status", None) or (
            pi_obj.get("status") if isinstance(pi_obj, dict) else None
        )
        # Only return client_secret for PIs that can be used with Stripe Elements.
        if pi_status not in ("requires_payment_method", "requires_confirmation", "requires_action"):
            logger.info(
                "membership_service: PI not actionable pi_id=%s status=%s", pi_id, pi_status
            )
            return None
        return getattr(pi_obj, "client_secret", None) or (
            pi_obj.get("client_secret") if isinstance(pi_obj, dict) else None
        )
    except stripe.StripeError as e:
        logger.warning("membership_service: PaymentIntent.retrieve failed pi_id=%s err=%s", pi_id, e)
        return None


def get_credits_remaining(membership, period_start=None):
    """
    Return number of credits remaining for this membership in the given period.
    period_start should be a date; if None, use membership.current_period_start date.
    Only meaningful when product.access_type == 'credits'.
    """
    if membership.product.access_type != "credits" or not membership.product.credit_allowance:
        return None
    period = period_start
    if period is None and membership.current_period_start:
        period = membership.current_period_start.date()
    if period is None:
        return membership.product.credit_allowance
    used = (
        MembershipCreditLedger.objects.filter(
            membership=membership,
            period_start=period,
            action="consumed",
        ).aggregate(total=Sum("credits_used"))["total"]
        or 0
    )
    allowance = membership.product.credit_allowance or 0
    return max(0, allowance - used)


def consume_credit(membership, booking, period_start=None):
    """
    Consume one credit (or more) for this membership and link to the booking.
    Raises ValueError if no credits remaining.
    """
    remaining = get_credits_remaining(membership, period_start)
    if remaining is None:
        return
    if remaining <= 0:
        raise ValueError("No credits remaining for this membership period")
    period = period_start
    if period is None and membership.current_period_start:
        period = membership.current_period_start.date()
    if period is None:
        raise ValueError("Membership has no period start; cannot consume credit")
    MembershipCreditLedger.objects.create(
        membership=membership,
        booking=booking,
        period_start=period,
        credits_used=1,
        action="consumed",
    )


def reset_period_credits(membership, period_start):
    """
    Record a ledger entry for period reset (e.g. on invoice.paid for new period).
    Does not change allowance; just logs the reset action. Optional.
    period_start: date or datetime for the new period start.
    """
    if hasattr(period_start, "date"):
        period_start = period_start.date()
    MembershipCreditLedger.objects.create(
        membership=membership,
        booking=None,
        period_start=period_start,
        credits_used=0,
        action="reset",
    )
