from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Max, Q
from django.template.loader import render_to_string
from django.conf import settings
from collections import defaultdict
from quickstart.models import BusinessInfo, ClassesMain
import logging
import resend

logger = logging.getLogger(__name__)

@shared_task
def notify_businesses_of_expiring_schedules():
    """
    Checks for active classes that have no future instances or 
    whose last instance is within the next 14 days.
    
    Sends a consolidated email to the business owner using Resend's Batch API
    to avoid rate limits and improve performance.
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
        business_map[cls.businessId].append({
            'title': cls.title,
            'last_date': cls.last_scheduled_date
        })

    # 3. Prepare the email batch list
    email_batch = []
    
    # Configure Resend API Key
    try:
        resend.api_key = settings.RESEND_API_KEY
    except AttributeError:
        logger.error("RESEND_API_KEY not found in settings.")
        return "Failed: Missing Resend API Key in settings."

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
        
        # Add email object to the batch list
        email_batch.append({
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": [owner.email],
            "subject": f"Action Required: {len(class_list)} of your classes are ending soon",
            "html": html_content,
        })

    if not email_batch:
        return "No valid business emails found to send."

    # 4. Send in chunks (Resend limits batches to 100 emails per request)
    BATCH_SIZE = 100
    sent_count = 0

    for i in range(0, len(email_batch), BATCH_SIZE):
        chunk = email_batch[i:i + BATCH_SIZE]
        try:
            resend.Batch.send(chunk)
            sent_count += len(chunk)
            logger.info(f"Successfully sent batch of {len(chunk)} schedule expiry emails.")
        except Exception as e:
            logger.error(f"Failed to send batch of emails (index {i} to {i+len(chunk)}): {e}")

    return f"Processed schedule expiry check. Sent {sent_count} emails via Resend Batch API."