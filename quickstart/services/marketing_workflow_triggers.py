"""Auto-enroll contacts into workflows from product events."""
import logging

from django.utils import timezone

from quickstart.models import (
    Contact,
    MarketingWorkflow,
    MarketingWorkflowEnrollment,
)

logger = logging.getLogger(__name__)


def try_auto_enroll_booking_completed(booking):
    """
    When a booking moves to completed, enroll the contact in matching workflows.
    Trigger type: booking_completed on MarketingWorkflow.
    """
    if booking.status != "completed":
        return
    try:
        business = booking.schedule_instance.schedule.option.classId.businessId
    except Exception:
        logger.warning("booking_completed workflow: missing schedule chain booking=%s", booking.pk)
        return

    contact = booking.contact
    if not contact and booking.user_id:
        contact = Contact.objects.filter(business=business, user_id=booking.user_id).first()
    if not contact or not (contact.email or "").strip() or contact.marketing_unsubscribed:
        return

    wfs = MarketingWorkflow.objects.filter(
        business=business,
        status="active",
        trigger_type="booking_completed",
    )
    for wf in wfs:
        MarketingWorkflowEnrollment.objects.get_or_create(
            workflow=wf,
            contact=contact,
            defaults={
                "current_step_index": 0,
                "next_run_at": timezone.now(),
                "status": "active",
            },
        )
