# quickstart/management/commands/fix_apple_pay_booking_contacts.py
"""
Fix bookings where Apple Pay (or other wallet) payment completed before form data
was sent, leaving the contact with template/placeholder data (e.g. pending@example).

Fetches correct name/email/phone from Stripe PaymentIntent (Charge billing_details)
and updates the Contact and booking participant_details.

Usage:
  python manage.py fix_apple_pay_booking_contacts              # dry run (default)
  python manage.py fix_apple_pay_booking_contacts --apply       # apply updates
"""

import logging
import stripe
from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.utils import IntegrityError

from quickstart.models import Booking, Contact, Payment

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


def is_placeholder_contact(contact):
    """True if contact has known placeholder/template data."""
    if not contact:
        return False
    email = (contact.email or "").strip().lower()
    if "pending@example" in email or email == "pending":
        return True
    if (contact.first_name or "").strip().lower() == "guest" and not contact.email:
        return True
    return False


def get_billing_details_from_stripe(payment_intent_id):
    """
    Retrieve billing name, email, phone from Stripe for a succeeded PaymentIntent.
    Uses latest_charge.billing_details; falls back to payment_method.billing_details.
    Returns dict with keys: name, email, phone (values may be None).
    """
    out = {"name": None, "email": None, "phone": None}
    try:
        intent = stripe.PaymentIntent.retrieve(
            payment_intent_id,
            expand=["latest_charge", "payment_method"],
        )
    except stripe.StripeError as e:
        logger.warning("Stripe error for %s: %s", payment_intent_id, e)
        return out

    # Prefer Charge billing_details (what was actually charged)
    charge = getattr(intent, "latest_charge", None)
    if charge:
        if isinstance(charge, str):
            try:
                charge = stripe.Charge.retrieve(charge)
            except stripe.StripeError:
                charge = None
        if charge and getattr(charge, "billing_details", None):
            bd = charge.billing_details
            if isinstance(bd, dict):
                out["name"] = (bd.get("name") or "").strip() or None
                out["email"] = (bd.get("email") or "").strip() or None
                out["phone"] = (bd.get("phone") or "").strip() or None
            else:
                out["name"] = (getattr(bd, "name", None) or "").strip() or None
                out["email"] = (getattr(bd, "email", None) or "").strip() or None
                out["phone"] = (getattr(bd, "phone", None) or "").strip() or None

    # Fallback: PaymentMethod billing_details (e.g. Apple Pay may set these)
    if not out["email"] or not out["name"]:
        pm = getattr(intent, "payment_method", None)
        if pm and getattr(pm, "billing_details", None):
            bd = pm.billing_details
            if isinstance(bd, dict):
                out["name"] = out["name"] or (bd.get("name") or "").strip() or None
                out["email"] = out["email"] or (bd.get("email") or "").strip() or None
                out["phone"] = out["phone"] or (bd.get("phone") or "").strip() or None
            else:
                out["name"] = out["name"] or (getattr(bd, "name", None) or "").strip() or None
                out["email"] = out["email"] or (getattr(bd, "email", None) or "").strip() or None
                out["phone"] = out["phone"] or (getattr(bd, "phone", None) or "").strip() or None

    # Receipt email on PaymentIntent as last resort for email
    if not out["email"] and getattr(intent, "receipt_email", None):
        out["email"] = (intent.receipt_email or "").strip() or None

    return out


def split_full_name(full_name):
    """Return (first_name, last_name) from a full name string."""
    if not full_name or not full_name.strip():
        return "Guest", ""
    parts = full_name.strip().split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


class Command(BaseCommand):
    help = (
        "Find bookings with placeholder contact data (e.g. pending@example) and update "
        "from Stripe PaymentIntent billing details. Use --apply to persist changes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply updates to Contact and booking. Default is dry run.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        if not apply:
            self.stdout.write(
                self.style.WARNING("DRY RUN: No changes will be written. Use --apply to update.")
            )

        # Bookings that have a contact with placeholder email and a succeeded payment with real Stripe PI
        placeholder_email_q = Q(contact__email__icontains="pending@example") | Q(
            contact__email__iexact="pending"
        )
        bookings = (
            Booking.objects.filter(placeholder_email_q)
            .filter(status__in=["confirmed", "completed"])
            .select_related("contact", "schedule_instance__schedule__option__classId__businessId")
            .order_by("booking_date")
        )

        # Restrict to those that have a succeeded payment with a Stripe PI id (starts with pi_)
        fixed = 0
        skipped_no_payment = 0
        skipped_no_stripe_id = 0
        skipped_no_billing = 0
        errors = 0

        for booking in bookings:
            contact = booking.contact
            if not contact or not is_placeholder_contact(contact):
                continue

            payment = (
                Payment.objects.filter(booking=booking, status="succeeded")
                .order_by("-created_at")
                .first()
            )
            if not payment:
                skipped_no_payment += 1
                self.stdout.write(
                    f"  Skip booking {booking.id} (ref {booking.user_facing_reference}): no succeeded payment"
                )
                continue

            pi_id = payment.stripe_payment_intent_id
            if not pi_id or not str(pi_id).startswith("pi_"):
                skipped_no_stripe_id += 1
                self.stdout.write(
                    f"  Skip booking {booking.id}: payment has no Stripe PI id ({pi_id})"
                )
                continue

            billing = get_billing_details_from_stripe(pi_id)
            if not billing.get("email") and not billing.get("name"):
                skipped_no_billing += 1
                self.stdout.write(
                    f"  Skip booking {booking.id}: no billing email/name in Stripe for {pi_id}"
                )
                continue

            first_name, last_name = split_full_name(billing.get("name"))
            new_email = billing.get("email") or contact.email
            new_phone = billing.get("phone") or contact.phone_number or ""

            if apply:
                try:
                    with transaction.atomic():
                        contact.first_name = first_name or "Guest"
                        contact.last_name = last_name or ""
                        contact.email = new_email
                        contact.phone_number = new_phone or ""
                        contact.save(update_fields=["first_name", "last_name", "email", "phone_number"])

                        # Update participant_details if they look like placeholder (single "Guest" etc.)
                        participants = booking.participants or 1
                        details = list(booking.participant_details or [])
                        if not details or len(details) != participants:
                            details = [{"name": f"{first_name} {last_name}".strip() or "Guest"} for _ in range(participants)]
                            booking.participant_details = details
                            booking.save(update_fields=["participant_details"])
                        else:
                            # Replace first participant name if it's generic
                            first_detail = details[0] if details else {}
                            if isinstance(first_detail, dict):
                                current_name = (first_detail.get("name") or "").strip().lower()
                                if current_name in ("guest", "pending", "") or "pending@example" in current_name:
                                    details[0] = {**first_detail, "name": f"{first_name} {last_name}".strip() or "Guest"}
                                    booking.participant_details = details
                                    booking.save(update_fields=["participant_details"])
                    fixed += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Updated booking {booking.id} (ref {booking.user_facing_reference}): "
                            f"contact {contact.id} -> {new_email}, {first_name} {last_name}"
                        )
                    )
                except IntegrityError as e:
                    errors += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"  Booking {booking.id}: contact save failed (duplicate email?): {e}"
                        )
                    )
                except Exception as e:
                    errors += 1
                    logger.exception("Failed to update booking %s", booking.id)
                    self.stdout.write(
                        self.style.ERROR(f"  Error updating booking {booking.id}: {e}")
                    )
            else:
                fixed += 1
                self.stdout.write(
                    f"  Would update booking {booking.id} (ref {booking.user_facing_reference}): "
                    f"contact {contact.email} -> {new_email}, '{first_name} {last_name}'"
                )

        self.stdout.write("-" * 60)
        self.stdout.write(
            f"Done. Would apply/Applied: {fixed} | No payment: {skipped_no_payment} | "
            f"No Stripe PI: {skipped_no_stripe_id} | No billing in Stripe: {skipped_no_billing} | Errors: {errors}"
        )
        if not apply and fixed:
            self.stdout.write(self.style.WARNING("Run with --apply to persist these updates."))
