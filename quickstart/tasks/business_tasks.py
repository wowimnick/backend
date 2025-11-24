from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Max, Q
from django.template.loader import render_to_string
from django.conf import settings
from collections import defaultdict
from quickstart.models import BusinessInfo, ClassesMain
from quickstart.tasks.email_tasks import send_transactional_email_task
import logging

logger = logging.getLogger(__name__)

@shared_task
def notify_businesses_of_expiring_schedules():
    """
    Checks for active classes that have no future instances or 
    whose last instance is within the next 14 days.
    Sends a consolidated email to the business owner.
    """
    today = timezone.now().date()
    warning_threshold = today + timedelta(days=14)
    
    logger.info("Starting schedule expiry check task.")

    # 1. Query active classes and annotate with the date of their *last* scheduled instance
    # We filter for classes where that max date is either None (no schedules) 
    # or less than our warning threshold.
    classes_needing_schedules = ClassesMain.objects.filter(
        status='active',
        businessId__isActive=True
    ).annotate(
        last_scheduled_date=Max('options__schedules__instances__date')
    ).filter(
        Q(last_scheduled_date__lte=warning_threshold) | Q(last_scheduled_date__isnull=True)
    ).select_related('businessId', 'businessId__owner')

    if not classes_needing_schedules.exists():
        return "No classes found needing schedule updates."

    # 2. Group classes by Business to send 1 email per business
    business_map = defaultdict(list)
    
    for cls in classes_needing_schedules:
        # If the class has options but no schedules ever created, last_scheduled_date is None.
        # If it has schedules but they are all in the past, last_scheduled_date is < today.
        # If it has future schedules but they run out soon, last_scheduled_date is < threshold.
        
        # Optimization: We can filter out classes that are 'active' but might be old/abandoned 
        # if they haven't had a schedule in > 1 year, depending on requirements. 
        # For now, we assume if it's marked 'active', the user cares about it.
        
        business_map[cls.businessId].append({
            'title': cls.title,
            'last_date': cls.last_scheduled_date
        })

    email_count = 0

    # 3. Iterate through grouped data and send emails
    for business, class_list in business_map.items():
        owner = business.owner
        if not owner or not owner.email:
            continue

        # Prepare context for the template
        context = {
            'business_user': owner,
            'expiring_classes': class_list,
            'dashboard_url': f"{settings.FRONTEND_BASE_URL}/business/classes",
            'settings': settings # To access frontend url in base template
        }

        html_content = render_to_string("emails/business_schedule_expiry_warning.html", context)
        
        # Send email
        try:
            send_transactional_email_task.delay(
                to=owner.email,
                subject=f"Action Required: {len(class_list)} of your classes are ending soon",
                html=html_content,
                from_email=settings.DEFAULT_FROM_EMAIL
            )
            email_count += 1
            logger.info(f"Queued schedule expiry warning for business {business.businessName} ({len(class_list)} classes)")
        except Exception as e:
            logger.error(f"Failed to queue schedule warning email for {owner.email}: {e}")

    return f"Processed schedule expiry check. Sent emails to {email_count} businesses."