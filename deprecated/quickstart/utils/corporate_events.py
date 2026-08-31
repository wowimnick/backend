"""Timeline events for corporate bookings."""

from quickstart.models import CorporateBookingEvent


def log_corporate_booking_event(booking, event_type, message="", user=None, metadata=None):
    CorporateBookingEvent.objects.create(
        booking=booking,
        event_type=event_type,
        message=message or "",
        created_by=user if user and user.is_authenticated else None,
        metadata=metadata or {},
    )
