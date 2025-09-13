from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum, Count, CharField
from django.db.models.functions import Coalesce, Cast
from decimal import Decimal

from quickstart.models import BusinessInfo, Booking
from quickstart.utils.email_utils import send_performance_summary_email
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_weekly_performance_summaries():
    """
    Sends a weekly performance summary to all active and verified businesses.
    """
    today = timezone.now().date()
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=6)  # Full 7 day period

    active_businesses = BusinessInfo.objects.filter(
        isActive=True, verificationStatus="verified"
    )
    logger.info(
        f"Starting weekly performance summary task for {active_businesses.count()} businesses."
    )

    for business in active_businesses:
        try:
            # Calculate stats for the last 7 days
            bookings_in_period = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business,
                booking_date__date__range=[start_date, end_date],
            )

            total_revenue = bookings_in_period.filter(
                status__in=["confirmed", "completed"], payment_status="paid"
            ).aggregate(total=Sum("amount_paid"))["total"] or Decimal("0.00")

            new_bookings_count = bookings_in_period.count()
            # MODIFIED: Correctly count unique users OR contacts
            unique_students_count = (
                bookings_in_period.annotate(
                    booker_identifier=Coalesce(
                        Cast("user_id", output_field=CharField()),
                        Cast("contact_id", output_field=CharField()),
                    )
                )
                .values("booker_identifier")
                .distinct()
                .count()
            )

            summary_data = {
                "total_revenue": f"{total_revenue:.2f}",
                "new_bookings": new_bookings_count,
                "unique_students": unique_students_count,
                "start_date": start_date.strftime("%b %d"),
                "end_date": end_date.strftime("%b %d, %Y"),
            }

            # Find all recipients (owner + managers)
            recipients = {business.owner} | set(business.managers.all())
            for user in recipients:
                if user and user.email:
                    send_performance_summary_email(
                        business_user=user,
                        summary_data=summary_data,
                        period="Weekly",
                    )
            logger.info(
                f"Queued weekly summary for business '{business.businessName}' (ID: {business.businessId})."
            )

        except Exception as e:
            logger.error(
                f"Failed to generate performance summary for business {business.businessId}: {e}",
                exc_info=True,
            )

    return f"Finished sending weekly performance summaries for {active_businesses.count()} businesses."
