from .payout_tasks import *
from .email_tasks import *
# Celery autodiscover_tasks() only imports this package (__init__); submodules are not auto-loaded.
# These modules define @shared_task handlers used by django-celery-beat / CELERY_BEAT_SCHEDULE.
from . import email_marketing_tasks  # noqa: F401
from . import email_marketing_workflow_tasks  # noqa: F401
from .user_tasks import *
from .business_tasks import *
from .booking_tasks import *
from .cache_tasks import *  # prewarm_class_search_cache_task (registered by name for workers)
from .giftcard_tasks import *
from .notification_tasks import send_sms_task, send_campaign_task
from . import corporate_tasks  # noqa: F401 — register send_corporate_inquiry_emails on workers

__all__ = [
    "process_daily_payouts",
    "update_completed_booking_status",
    "process_daily_refunds",
    "send_transactional_email_task",
    "send_upcoming_booking_reminders",
    "send_email_task",
    "send_bulk_emails_task",
    "send_pending_review_requests",
    "prewarm_class_search_cache_task",
    "process_scheduled_gift_cards",
    "send_sms_task",
    "send_campaign_task",
]
