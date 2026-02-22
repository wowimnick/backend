# quickstart/tasks/notification_tasks.py
"""
Celery tasks for notification campaigns (email + SMS) and transactional SMS.
"""
import logging
from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from django.core.cache import cache
from django.conf import settings

from quickstart.models import (
    NotificationCampaign,
    UserSegment,
    CustomUser,
    Role,
    ClassCategory,
    Booking,
)
from quickstart.utils.sms_utils import send_sms, normalize_phone_for_sns
from quickstart.tasks.email_tasks import send_transactional_email_task

logger = logging.getLogger(__name__)

PROGRESS_CACHE_KEY_PREFIX = "notification_campaign_progress_"


def get_progress_cache_key(campaign_id):
    return f"{PROGRESS_CACHE_KEY_PREFIX}{campaign_id}"


@shared_task(bind=True)
def send_sms_task(self, phone_number: str, message: str):
    """
    Send one SMS via AWS SNS (delegates to sms_utils.send_sms).
    Optional: log to AuditLog (can be added later).
    """
    return send_sms(phone_number, message)


def _get_segment_users_queryset(segment):
    """Resolve users for a segment (mirrors AdminNotificationCampaignViewSet._get_segment_users)."""
    segment_name = segment.name
    if segment_name == "all_users":
        return CustomUser.objects.filter(is_active=True)
    if ":" in segment_name:
        segment_type, segment_value = segment_name.split(":", 1)
        if segment_type == "role":
            role = Role.objects.filter(name=segment_value).first()
            return CustomUser.objects.filter(role=role, is_active=True) if role else CustomUser.objects.none()
        if segment_type == "category":
            category = ClassCategory.objects.filter(name=segment_value).first()
            if category:
                return CustomUser.objects.filter(
                    bookings__schedule_instance__schedule__option__classId__category=category,
                    is_active=True,
                ).distinct()
            return CustomUser.objects.none()
    if segment_name == "new_users":
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return CustomUser.objects.filter(createdAt__gte=thirty_days_ago, is_active=True)
    if segment_name == "inactive_users":
        sixty_days_ago = timezone.now() - timedelta(days=60)
        active_ids = Booking.objects.filter(booking_date__gte=sixty_days_ago).values_list("user_id", flat=True).distinct()
        return CustomUser.objects.filter(bookings__isnull=False, is_active=True).exclude(userId__in=active_ids).distinct()
    if isinstance(segment.criteria, dict) and segment.criteria:
        qs = CustomUser.objects.filter(is_active=True)
        if "role" in segment.criteria:
            role_id = segment.criteria["role"]
            if isinstance(role_id, int):
                qs = qs.filter(role_id=role_id)
            elif isinstance(role_id, str):
                qs = qs.filter(role__name=role_id)
        if "joined_after" in segment.criteria:
            try:
                date_str = segment.criteria["joined_after"]
                date = timezone.datetime.fromisoformat(date_str) if "T" in date_str else timezone.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                qs = qs.filter(createdAt__gte=date)
            except (ValueError, TypeError):
                pass
        return qs.distinct()
    return CustomUser.objects.none()


def _get_campaign_recipients(campaign):
    """Return queryset of CustomUser for the campaign's audience."""
    if campaign.audience_type == "all_users":
        return CustomUser.objects.filter(is_active=True)
    if campaign.audience_type == "segment" and campaign.segment:
        segment = UserSegment.objects.filter(name=campaign.segment).first()
        if not segment and str(campaign.segment).isdigit():
            segment = UserSegment.objects.filter(id=int(campaign.segment)).first()
        if segment:
            return _get_segment_users_queryset(segment)
        return CustomUser.objects.none()
    if campaign.audience_type == "individual" and campaign.target_user_ids:
        return CustomUser.objects.filter(userId__in=campaign.target_user_ids, is_active=True)
    return CustomUser.objects.none()


@shared_task(bind=True)
def send_campaign_task(self, campaign_id, template_variables=None):
    """
    Send a notification campaign (email or SMS). Resolves recipients, sends, updates campaign and progress cache.
    """
    template_variables = template_variables or {}
    try:
        campaign = NotificationCampaign.objects.get(pk=campaign_id)
    except NotificationCampaign.DoesNotExist:
        logger.error("send_campaign_task: campaign %s not found", campaign_id)
        return
    if campaign.status not in ("draft", "sending"):
        logger.warning("send_campaign_task: campaign %s status=%s, skipping", campaign_id, campaign.status)
        return

    cache_key = get_progress_cache_key(campaign_id)
    recipients = _get_campaign_recipients(campaign)
    recipients = list(recipients.distinct())
    total = len(recipients)
    campaign.recipient_count = total
    campaign.save(update_fields=["recipient_count"])
    cache.set(cache_key, {"status": "sending", "processed": 0, "total": total, "message": "Starting..."}, timeout=3600)

    if campaign.notification_type == "email":
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@classeasily.com")
        subject = campaign.subject or "Notification from ClassEasily"
        html = campaign.html_content or campaign.content or ""
        sent = 0
        for i, user in enumerate(recipients):
            if not getattr(user, "email", None):
                continue
            try:
                send_transactional_email_task.delay(
                    to=[user.email],
                    subject=subject,
                    html=html,
                    from_email=from_email,
                )
                sent += 1
            except Exception as e:
                logger.exception("Campaign %s email to %s failed: %s", campaign_id, user.email, e)
            cache.set(cache_key, {"status": "sending", "processed": i + 1, "total": total, "message": "Sending..."}, timeout=3600)
        campaign.delivered_count = sent
        campaign.success_rate = (sent / total * 100) if total else 0.0
        campaign.status = "sent"
        campaign.sent_at = timezone.now()
        campaign.save(update_fields=["delivered_count", "success_rate", "status", "sent_at"])
        cache.set(cache_key, {"status": "complete", "processed": total, "total": total}, timeout=3600)
        logger.info("Campaign %s email send complete: %s/%s", campaign_id, sent, total)
        return

    if campaign.notification_type == "sms":
        if not getattr(settings, "AWS_SMS_ENABLED", False):
            campaign.status = "failed"
            campaign.error_message = "SMS is not enabled (AWS_SMS_ENABLED)."
            campaign.save(update_fields=["status", "error_message"])
            cache.set(cache_key, {"status": "error", "message": campaign.error_message}, timeout=3600)
            return
        content = (campaign.content or "").strip() or "ClassEasily notification."
        if len(content) > 1600:
            content = content[:1597] + "..."
        recipients_info = []
        for user in recipients:
            phone = getattr(user, "phone_number", None) or ""
            normalized = normalize_phone_for_sns(phone)
            if normalized:
                recipients_info.append({"user_id": getattr(user, "userId", user.pk), "email": getattr(user, "email", ""), "phone_number": normalized})
        delivered = 0
        for i, r in enumerate(recipients_info):
            try:
                send_sms_task.delay(r["phone_number"], content)
                delivered += 1
            except Exception as e:
                logger.exception("Campaign %s SMS to %s failed: %s", campaign_id, r["phone_number"], e)
            cache.set(cache_key, {"status": "sending", "processed": i + 1, "total": len(recipients_info), "message": "Sending SMS..."}, timeout=3600)
        total_sms = len(recipients_info)
        campaign.delivered_count = delivered
        campaign.success_rate = (delivered / total_sms * 100) if total_sms else 0.0
        campaign.status = "sent"
        campaign.sent_at = timezone.now()
        campaign.save(update_fields=["delivered_count", "success_rate", "status", "sent_at"])
        cache.set(cache_key, {"status": "complete", "processed": total_sms, "total": total_sms}, timeout=3600)
        logger.info("Campaign %s SMS send complete: %s/%s", campaign_id, delivered, total_sms)
        return

    campaign.status = "failed"
    campaign.error_message = f"Unsupported notification_type: {campaign.notification_type}"
    campaign.save(update_fields=["status", "error_message"])
    cache.set(cache_key, {"status": "error", "message": campaign.error_message}, timeout=3600)
