# quickstart/tasks.py
import logging
from celery import shared_task
from django.core.mail import EmailMultiAlternatives
from django.conf import settings

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3, default_retry_delay=60) # bind=True allows access to self, add retry logic
def send_email_task(self, subject, body, from_email, to_list, reply_to_list=None, html_content=None, attachments=None, **kwargs):
    """
    Celery task to send an email asynchronously using Django's mail functions.
    Handles potential exceptions and retries.
    """
    logger.info(f"Task send_email_task received: Subject='{subject}', To={to_list}")
    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=to_list,
            reply_to=reply_to_list # Pass reply_to if provided
            # attachments=attachments # TODO: Handle attachments if needed
        )
        if html_content:
            msg.attach_alternative(html_content, "text/html")

        # TODO: Add attachment handling if your email utility passes them
        # if attachments:
        #    for attachment_data in attachments:
        #        # Assuming attachment_data is {'filename': '...', 'content': bytes, 'mimetype': '...'}
        #        msg.attach(attachment_data['filename'], attachment_data['content'], attachment_data.get('mimetype'))

        result = msg.send(fail_silently=False) # Set fail_silently=False to catch errors for retry
        logger.info(f"Email task successful for Subject='{subject}', To={to_list}. Result: {result}")
        return result # Return number of emails sent

    except Exception as exc:
        logger.error(f"Email task failed for Subject='{subject}', To={to_list}. Error: {exc}", exc_info=True)
        # Retry the task using Celery's built-in mechanism
        # Exponential backoff might be added to default_retry_delay calculation
        try:
            # self.request.retries gives the current retry count
            retry_count = self.request.retries
            logger.warning(f"Retrying email task (attempt {retry_count + 1}/{self.max_retries})...")
            # Raise the exception to trigger Celery's retry mechanism
            # It will use the default_retry_delay specified in the decorator
            raise self.retry(exc=exc, countdown=60 * (2 ** retry_count)) # Exponential backoff example
        except self.MaxRetriesExceededError:
            logger.critical(f"Email task failed permanently after {self.max_retries} retries for Subject='{subject}', To={to_list}.")
            # Optionally: Send notification to admin about permanent failure
            return f"Failed after {self.max_retries} retries."