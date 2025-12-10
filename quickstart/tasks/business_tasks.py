from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Max, Q
from django.template.loader import render_to_string
from django.conf import settings
from django.core.cache import cache
from collections import defaultdict
from quickstart.models import BusinessInfo, ClassesMain
import logging
import resend

logger = logging.getLogger(__name__)

@shared_task
def notify_businesses_of_expiring_schedules():
    """
    Checks for active classes that need new schedules.
    
    ZERO SPAM GUARANTEE:
    Locks each business ID in cache for 24 hours to prevent duplicate 
    emails if task is re-run or retried.
    """
    today = timezone.now().date()
    warning_threshold = today + timedelta(days=14)
    
    logger.info("Starting schedule expiry check task.")

    # 1. Query active classes
    classes_needing_schedules = ClassesMain.objects.filter(
        status='active',
        businessId__isActive=True,
        businessId__scheduleExpiryNotification=True
    ).annotate(
        last_scheduled_date=Max('options__schedules__instances__date')
    ).filter(
        Q(last_scheduled_date__lte=warning_threshold) | Q(last_scheduled_date__isnull=True)
    ).select_related('businessId', 'businessId__owner')

    if not classes_needing_schedules.exists():
        return "No classes found needing schedule updates."

    # 2. Group by Business
    business_map = defaultdict(list)
    for cls in classes_needing_schedules:
        business_map[cls.businessId].append({
            'title': cls.title,
            'last_date': cls.last_scheduled_date
        })

    # 3. Prepare Email Batch
    email_batch = []
    
    try:
        resend.api_key = settings.RESEND_API_KEY
    except AttributeError:
        return "Failed: Missing Resend API Key."

    for business, class_list in business_map.items():
        # ATOMIC LOCK: Lock this business for 24 hours
        cache_key = f"schedule_expiry_alert_{business.businessId}_{today}"
        
        if not cache.add(cache_key, True, timeout=86400):
            logger.info(f"Skipping business {business.businessId} (Email already queued today)")
            continue

        owner = business.owner
        if not owner or not owner.email:
            continue

        context = {
            'business_user': owner,
            'expiring_classes': class_list,
            'dashboard_url': f"{settings.FRONTEND_BASE_URL}/business/classes",
            'settings_url': f"{settings.FRONTEND_BASE_URL}/business/dashboard",
            'settings': settings 
        }

        html_content = render_to_string("emails/business_schedule_expiry_warning.html", context)
        
        email_batch.append({
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": [owner.email],
            "subject": f"Action Required: {len(class_list)} of your classes are ending soon",
            "html": html_content,
        })

    if not email_batch:
        return "No valid business emails found to send (or all locked)."

    # 4. Send in chunks
    BATCH_SIZE = 100
    sent_count = 0

    for i in range(0, len(email_batch), BATCH_SIZE):
        chunk = email_batch[i:i + BATCH_SIZE]
        try:
            resend.Batch.send(chunk)
            sent_count += len(chunk)
            logger.info(f"Successfully sent batch of {len(chunk)} schedule expiry emails.")
        except Exception as e:
            # Note: We do NOT release the locks. 
            # If the batch fails, we assume it might have partially sent. 
            # Better to miss an email than spam.
            logger.error(f"Failed to send batch (index {i}): {e}")

    return f"Processed schedule expiry. Sent {sent_count} emails."