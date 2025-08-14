# quickstart/tasks/email_tasks.py
import time
import logging
from celery import shared_task
from celery.exceptions import Retry
from django.conf import settings
import resend
from resend.exceptions import ResendError, ValidationError

logger = logging.getLogger(__name__)

# Rate limiting configuration for Resend API
RESEND_RATE_LIMIT = 2  # requests per second
RATE_LIMIT_WINDOW = 1  # second


class RateLimitedEmailSender:
    """Thread-safe rate limiter for email sending"""

    def __init__(self):
        self.last_request_time = 0
        self.request_count = 0
        self.window_start = 0

    def wait_if_needed(self):
        """Implement rate limiting by waiting if necessary"""
        current_time = time.time()

        # Reset window if needed
        if current_time - self.window_start >= RATE_LIMIT_WINDOW:
            self.window_start = current_time
            self.request_count = 0

        # If we've hit the limit, wait until the window resets
        if self.request_count >= RESEND_RATE_LIMIT:
            sleep_time = RATE_LIMIT_WINDOW - (current_time - self.window_start)
            if sleep_time > 0:
                time.sleep(sleep_time)
                # Reset after waiting
                self.window_start = time.time()
                self.request_count = 0

        self.request_count += 1


# Global rate limiter instance
rate_limiter = RateLimitedEmailSender()


@shared_task(
    bind=True,
    autoretry_for=(ResendError, ConnectionError, TimeoutError),
    retry_kwargs={"max_retries": 5, "countdown": 30},
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def send_transactional_email_task(self, **kwargs):
    """
    Send transactional email with rate limiting and retry logic.

    Args:
        **kwargs: Email parameters including 'to', 'subject', 'html', etc.
    """
    try:
        # Apply rate limiting before making the request
        rate_limiter.wait_if_needed()

        # Validate subject length before sending
        subject = kwargs.get("subject", "")
        if len(subject) > 1900:  # Leave some buffer below 2000 char limit
            # Truncate subject if too long
            kwargs["subject"] = subject[:1900] + "..."
            logger.warning(
                f"Subject truncated for email to {kwargs.get('to', 'unknown')}"
            )

        # Set up Resend with API key
        resend.api_key = settings.RESEND_API_KEY

        # Create email parameters - ensure 'to' field is properly handled
        to_recipients = kwargs.get("to", [])
        if not to_recipients:
            raise ValidationError("Missing 'to' field in email parameters")

        # Ensure to_recipients is a list
        if isinstance(to_recipients, str):
            to_recipients = [to_recipients]

        params = {
            "from": kwargs.get("from", settings.DEFAULT_FROM_EMAIL),
            "to": to_recipients,
            "subject": kwargs.get("subject", "Notification from ClassEasily"),
            "html": kwargs.get("html", ""),
        }

        # Add optional parameters if present
        if kwargs.get("text"):
            params["text"] = kwargs["text"]
        if kwargs.get("attachments"):
            params["attachments"] = kwargs["attachments"]
        if kwargs.get("reply_to"):
            params["reply_to"] = kwargs["reply_to"]

        # Log the email parameters for debugging (without sensitive data)
        logger.debug(
            f"Sending email with params: to={params['to']}, subject='{params['subject'][:50]}...', from={params['from']}"
        )

        # Send the email
        email = resend.Emails.send(params)

        # FIX: Handle both object and dict responses from Resend API
        if hasattr(email, "id"):
            # If email is an object with id attribute
            email_id = email.id
        elif isinstance(email, dict) and "id" in email:
            # If email is a dictionary with id key
            email_id = email["id"]
        else:
            # Fallback - log the response and use a placeholder
            logger.warning(f"Unexpected email response format: {type(email)} - {email}")
            email_id = "unknown"

        logger.info(
            f"Transactional email sent to {params['to']}. Resend ID: {email_id}"
        )

        return {"status": "success", "to": params["to"], "resend_id": email_id}

    except ValidationError as e:
        # Don't retry validation errors - they won't succeed on retry
        logger.error(
            f"Transactional email validation failed for {kwargs.get('to', 'unknown')}. Error: {str(e)}"
        )
        return {"status": "failed", "error": str(e), "to": kwargs.get("to", [])}

    except ResendError as e:
        # Check if it's a rate limit error
        if "Too many requests" in str(e) or "rate limit" in str(e).lower():
            logger.warning(
                f"Rate limit hit for {kwargs.get('to', 'unknown')}. Retrying in {self.default_retry_delay} seconds."
            )
            # Increase the retry delay for rate limit errors
            raise self.retry(countdown=60, max_retries=5)
        else:
            logger.error(
                f"Transactional email task FAILED for {kwargs.get('to', 'unknown')}. Error: {str(e)}"
            )
            raise

    except Exception as e:
        logger.error(
            f"Unexpected error sending email to {kwargs.get('to', 'unknown')}: {str(e)}"
        )
        raise


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 10},
)
def send_email_task(self, recipient_list, subject, message, from_email=None, **kwargs):
    """
    Legacy email task for compatibility.
    """
    html_message = kwargs.get("html_message", message)

    return send_transactional_email_task.delay(
        to=recipient_list,
        subject=subject,
        html=html_message,
        text=message,
        **{"from": from_email or settings.DEFAULT_FROM_EMAIL},
    )


# Batch email sending for multiple emails with automatic rate limiting
@shared_task(bind=True)
def send_bulk_emails_task(self, email_list, delay_between_emails=0.5):
    """
    Send multiple emails with rate limiting between each send.

    Args:
        email_list: List of email parameter dictionaries
        delay_between_emails: Additional delay between emails (in seconds)
    """
    results = []

    for i, email_params in enumerate(email_list):
        try:
            # Send each email as a separate task
            result = send_transactional_email_task.delay(**email_params)
            results.append(
                {
                    "index": i,
                    "task_id": result.id,
                    "email_to": email_params.get("to", []),
                }
            )

            # Additional delay between emails if specified
            if delay_between_emails > 0:
                time.sleep(delay_between_emails)

        except Exception as e:
            logger.error(f"Failed to queue email {i}: {str(e)}")
            results.append(
                {"index": i, "error": str(e), "email_to": email_params.get("to", [])}
            )

    return {"status": "completed", "total_emails": len(email_list), "results": results}
