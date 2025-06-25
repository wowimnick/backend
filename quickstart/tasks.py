# quickstart/tasks.py

import logging
from celery import shared_task, group
from django.core.cache import cache
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from .models import CustomUser, NotificationCampaign
from .views.admin.notifications.utils import generate_unsubscribe_token
import resend

logger = logging.getLogger(__name__)
resend.api_key = settings.RESEND_API_KEY

def get_progress_cache_key(campaign_id):
    """Helper to get the standardized Redis cache key."""
    return f"campaign_progress_{campaign_id}"

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_transactional_email_task(self, subject, from_email, to_list, html_content, reply_to_list=None, attachments=None):
    """
    A dedicated task for sending simple, transactional emails.
    """
    try:
        params = {
            "from": from_email,
            "to": to_list,
            "subject": subject,
            "html": html_content,
            "reply_to": reply_to_list,
            "attachments": attachments or [], # Ensure attachments is a list
        }
        # Filter out None values from the payload
        params = {k: v for k, v in params.items() if v is not None}

        email = resend.Emails.send(params)
        logger.info(f"Transactional email sent to {to_list}. Resend ID: {email['id']}")
        return {"status": "success", "to": to_list, "resend_id": email['id']}

    except Exception as exc:
        logger.error(f"Transactional email task FAILED for {to_list}. Error: {exc}", exc_info=True)
        # Retry the task if it fails
        raise self.retry(exc=exc)
    
@shared_task(bind=True)
def send_campaign_task(self, campaign_id, template_variables):
    """
    Master task to manage sending a notification campaign.
    It fetches recipients, dispatches individual email tasks, and tracks progress in Redis.
    """
    cache_key = get_progress_cache_key(campaign_id)
    try:
        campaign = NotificationCampaign.objects.get(id=campaign_id)
        # Update campaign status to 'sending'
        campaign.status = 'sending'
        campaign.celery_task_id = self.request.id
        campaign.save(update_fields=['status', 'celery_task_id'])

        # Fetch all recipient emails at once to avoid multiple DB hits
        # This part is reused from your view, but now it's in the background
        if campaign.audience_type == 'all_users':
            recipients_qs = CustomUser.objects.filter(is_active=True, is_unsubscribed=False)
        elif campaign.audience_type == 'segment' and campaign.segment:
            # You need a way to get the segment user queryset here.
            # This is a simplification. A real implementation would need the same
            # logic from your AdminNotificationCampaignViewSet._get_segment_users.
            # For now, let's assume a simplified lookup.
            from .views.admin.notifications.notification_views import AdminNotificationCampaignViewSet
            segment_logic_helper = AdminNotificationCampaignViewSet()
            recipients_qs = segment_logic_helper._get_segment_users_by_id(campaign.segment).filter(is_active=True, is_unsubscribed=False)
        elif campaign.audience_type == 'individual' and campaign.target_user_ids:
            recipients_qs = CustomUser.objects.filter(userId__in=campaign.target_user_ids, is_active=True, is_unsubscribed=False)
        else:
            recipients_qs = CustomUser.objects.none()

        # Get the final list of user data (id, email, name)
        recipients_data = list(recipients_qs.values('userId', 'email', 'first_name'))
        total_recipients = len(recipients_data)
        
        # Update recipient count in the DB
        campaign.recipient_count = total_recipients
        campaign.save(update_fields=['recipient_count'])

        logger.info(f"Campaign {campaign_id}: Found {total_recipients} valid recipients. Starting dispatch.")
        cache.set(cache_key, {'total': total_recipients, 'processed': 0, 'status': 'sending'}, timeout=86400)

        # Prepare common email data once
        common_data = {
            'subject': campaign.subject,
            'body_template': campaign.content,
            'html_template': campaign.html_content or "",
            'campaign_id': str(campaign_id), # Ensure it's a string
        }

        # Dispatch individual email tasks
        # Using Celery's `group` can be more efficient for managing a large set of tasks
        tasks_to_run = [
            send_email_task.s(user_data=recipient, common_data=common_data)
            for recipient in recipients_data
        ]
        
        task_group = group(tasks_to_run)
        group_result = task_group.apply_async()
        
        # Now, monitor the group task progress
        while not group_result.ready():
            completed_count = group_result.completed_count()
            cache.set(cache_key, {'total': total_recipients, 'processed': completed_count, 'status': 'sending'}, timeout=86400)
            logger.debug(f"Campaign {campaign_id} progress: {completed_count}/{total_recipients}")
            # Sleep for a bit to avoid hammering Redis and the result backend
            import time
            time.sleep(2)

        # Final update after the group is finished
        final_processed = group_result.completed_count()
        cache.set(cache_key, {'total': total_recipients, 'processed': final_processed, 'status': 'complete'}, timeout=3600) # Keep for 1 hour
        
        # Mark campaign as sent. Final stats will come from webhooks.
        campaign.status = 'sent'
        campaign.sent_at = timezone.now()
        campaign.save(update_fields=['status', 'sent_at'])
        logger.info(f"Campaign {campaign_id} finished sending. {final_processed} tasks completed.")

    except NotificationCampaign.DoesNotExist:
        logger.error(f"Campaign {campaign_id} not found for sending.")
        cache.set(cache_key, {'total': 0, 'processed': 0, 'status': 'error', 'message': 'Campaign not found'}, timeout=3600)
    except Exception as e:
        logger.error(f"Critical error in send_campaign_task for campaign {campaign_id}: {e}", exc_info=True)
        cache.set(cache_key, {'total': 0, 'processed': 0, 'status': 'error', 'message': str(e)}, timeout=3600)
        # Mark campaign as failed in DB
        NotificationCampaign.objects.filter(id=campaign_id).update(status='failed', error_message=str(e))
        raise

@shared_task(bind=True, max_retries=3, default_retry_delay=60, acks_late=True)
def send_email_task(self, user_data, common_data):
    """
    Sends a single, personalized email using Resend.
    This task is designed to be dispatched by the master campaign task.
    """
    recipient_email = user_data.get('email')
    recipient_name = user_data.get('first_name') or 'there'
    user_id = user_data.get('userId')
    campaign_id = common_data.get('campaign_id')

    logger.debug(f"Executing send_email_task for {recipient_email} in campaign {campaign_id}")

    try:
        # Generate unsubscribe token
        unsubscribe_token = generate_unsubscribe_token(user_id)
        unsubscribe_url = settings.FRONTEND_BASE_URL + reverse('notification-unsubscribe', kwargs={'token': unsubscribe_token})

        # Personalize content
        placeholders = {
            '{{name}}': recipient_name,
            '{{email}}': recipient_email,
            '{{unsubscribe_url}}': unsubscribe_url,
        }
        
        html_body = common_data['html_template']
        text_body = common_data['body_template']
        for key, value in placeholders.items():
            html_body = html_body.replace(key, value)
            text_body = text_body.replace(key, value)

        # Build Resend payload
        notification_settings = getattr(settings, 'NOTIFICATION_SETTINGS', {})
        from_email = f"{notification_settings.get('default_from_name', 'ClassEasily')} <{notification_settings.get('default_from_email', 'noreply@classeasily.com')}>"
        
        params = {
            "from": from_email,
            "to": [recipient_email],
            "subject": common_data['subject'],
            "html": html_body,
            "text": text_body,
            "headers": {
                "X-Campaign-ID": campaign_id,
            },
        }

        email = resend.Emails.send(params)
        logger.info(f"Email sent to {recipient_email} for campaign {campaign_id}. Resend ID: {email['id']}")
        return {"status": "success", "email": recipient_email}

    except Exception as exc:
        logger.error(f"Email task FAILED for {recipient_email}, campaign {campaign_id}. Error: {exc}", exc_info=True)
        raise self.retry(exc=exc)