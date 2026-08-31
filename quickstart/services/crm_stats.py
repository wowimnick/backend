"""Denormalized CRM stats for Contact records."""
import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Min, Q, Sum

logger = logging.getLogger(__name__)

CRM_BOOKING_STATUSES = ("confirmed", "completed")


def refresh_contact_stats(contact, Booking=None):
    """
    Recompute lifetime_value, booking_count, first/last booking, and last_activity
    from this contact's paid/confirmed/completed bookings.
    """
    if not contact:
        return contact

    if Booking is None:
        from quickstart.models import Booking as Booking

    qs = Booking.objects.filter(
        contact=contact,
        status__in=CRM_BOOKING_STATUSES,
    )
    agg = qs.aggregate(
        ltv=Sum("amount_paid", filter=Q(payment_status="paid")),
        count=Count("id"),
        first_at=Min("booking_date"),
        last_at=Max("booking_date"),
    )
    contact.lifetime_value = agg["ltv"] or Decimal("0.00")
    contact.booking_count = agg["count"] or 0
    contact.first_booking_at = agg["first_at"]
    contact.last_booking_at = agg["last_at"]
    contact.last_activity_at = agg["last_at"]
    contact.save(
        update_fields=[
            "lifetime_value",
            "booking_count",
            "first_booking_at",
            "last_booking_at",
            "last_activity_at",
            "updated_at",
        ]
    )
    return contact


def refresh_contact_stats_safe(contact):
    """Same as refresh_contact_stats but never raises (widget booking path)."""
    try:
        return refresh_contact_stats(contact)
    except Exception:
        logger.exception(
            "Failed to refresh CRM stats for contact %s",
            getattr(contact, "id", None),
        )
        return contact


def _booking_business(booking):
    try:
        return booking.schedule_instance.schedule.option.classId.businessId
    except AttributeError:
        return None


def _get_or_create_contact(Contact, business, user, email):
    email_norm = (email or "").strip() or None
    if user:
        existing = Contact.objects.filter(business=business, user=user).first()
        if existing:
            return existing, False
    if email_norm:
        existing = Contact.objects.filter(
            business=business, email__iexact=email_norm
        ).first()
        if existing:
            if user and not existing.user:
                existing.user = user
                existing.save(update_fields=["user"])
            return existing, False
    if not user and not email_norm:
        return None, False
    try:
        with transaction.atomic():
            contact = Contact.objects.create(
                business=business,
                user=user,
                email=email_norm,
                first_name=(getattr(user, "first_name", None) or "") if user else "",
                last_name=(getattr(user, "last_name", None) or "") if user else "",
                phone_number=(getattr(user, "phone_number", None) or "") if user else "",
                source="booking_backfill",
            )
        return contact, True
    except IntegrityError:
        if user:
            existing = Contact.objects.filter(business=business, user=user).first()
            if existing:
                return existing, False
        if email_norm:
            existing = Contact.objects.filter(
                business=business, email__iexact=email_norm
            ).first()
            if existing:
                return existing, False
        raise


def backfill_contacts_and_stats(Booking, Contact):
    """
    Link orphan bookings to contacts, then recompute CRM stats.
    Accepts historical or live models so migrations can call this.
    """
    linked = 0
    created = 0
    skipped = 0

    bookings = (
        Booking.objects.filter(contact__isnull=True, user__isnull=False)
        .exclude(user__email="")
        .select_related(
            "user",
            "schedule_instance__schedule__option__classId__businessId",
        )
        .iterator(chunk_size=200)
    )

    for booking in bookings:
        user = booking.user
        email = (getattr(user, "email", None) or "").strip() if user else ""
        if not user and not email:
            skipped += 1
            continue
        business = _booking_business(booking)
        if not business:
            skipped += 1
            continue
        contact, was_created = _get_or_create_contact(Contact, business, user, email)
        if not contact:
            skipped += 1
            continue
        booking.contact = contact
        booking.save(update_fields=["contact"])
        linked += 1
        if was_created:
            created += 1

    refreshed = 0
    for contact in Contact.objects.iterator(chunk_size=200):
        refresh_contact_stats(contact, Booking=Booking)
        refreshed += 1

    return {
        "linked": linked,
        "created": created,
        "skipped": skipped,
        "refreshed": refreshed,
    }
