# quickstart/tasks.py
import logging
from celery import shared_task
from django.core.mail import EmailMultiAlternatives
from django.conf import settings

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_email_task(self, subject, body, from_email, to_list, reply_to_list=None, html_content=None, attachments=None, **kwargs):
    """
    Celery task to send an email asynchronously using Django's mail functions.
    Handles potential exceptions and retries.
    `attachments` should be a list of dicts:
    [{'filename': 'event.ics', 'content': 'ICS_CONTENT_STRING', 'mimetype': 'text/calendar'}]
    """
    logger.info(f"Task send_email_task received: Subject='{subject}', To={to_list}, Attachments: {'Yes' if attachments else 'No'}")
    # Added detailed logging for attachments argument itself
    logger.debug(f"send_email_task: Initial attachments type: {type(attachments)}, value (first item if list): {attachments[0] if isinstance(attachments, list) and attachments else attachments}")

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body, # Plain text version
            from_email=from_email,
            to=to_list,
            reply_to=reply_to_list
        )
        if html_content:
            msg.attach_alternative(html_content, "text/html")

        if attachments:
            logger.debug(f"send_email_task: Processing attachments. Full attachments list: {attachments}")
            if not isinstance(attachments, list):
                logger.warning(f"Attachments for email '{subject}' is not a list, skipping. Got: {type(attachments)}")
            else:
                for i, attachment_data in enumerate(attachments):
                    logger.debug(f"send_email_task: Processing attachment #{i+1}/{len(attachments)}: {attachment_data.get('filename', 'N/A')}")
                    
                    if isinstance(attachment_data, dict) and \
                       'filename' in attachment_data and \
                       'content' in attachment_data:
                        
                        filename = attachment_data['filename']
                        content = attachment_data['content']
                        mimetype = attachment_data.get('mimetype')

                        logger.debug(f"send_email_task: Attachment #{i+1} - Filename: {filename}, Content type: {type(content)}, Mimetype: {mimetype}")
                        
                        if content is None:
                            logger.warning(f"send_email_task: Attachment #{i+1} ('{filename}') has None content. Skipping this attachment.")
                            continue

                        content_bytes = None 
                        if isinstance(content, str):
                            # All string content will be encoded to UTF-8
                            logger.debug(f"send_email_task: Attachment #{i+1} ('{filename}') - Content is string. Encoding to UTF-8.")
                            try:
                                content_bytes = content.encode('utf-8')
                                logger.debug(f"send_email_task: Attachment #{i+1} ('{filename}') - UTF-8 encoding successful. Bytes length: {len(content_bytes)}")
                            except UnicodeEncodeError as ue_err:
                                logger.error(f"Could not encode attachment content for '{filename}' to UTF-8: {ue_err}. Skipping attachment.")
                                continue
                        elif isinstance(content, bytes):
                            content_bytes = content
                            logger.debug(f"send_email_task: Attachment #{i+1} ('{filename}') - Content is already bytes. Length: {len(content_bytes)}")
                        else:
                            logger.error(f"Attachment content for '{filename}' is not a string or bytes. Skipping attachment. Type: {type(content)}")
                            continue
                        
                        if content_bytes is not None: # Should always be true if we didn't continue
                            if len(content_bytes) == 0:
                                logger.warning(f"send_email_task: Attachment #{i+1} ('{filename}') - Content bytes are empty after processing. Attachment might be empty or invalid.")
                            
                            logger.debug(f"send_email_task: Attempting to msg.attach: Filename='{filename}', Mimetype='{mimetype}', content_bytes length={len(content_bytes)}")
                            try:
                                msg.attach(filename, content_bytes, mimetype)
                                # More specific success log for msg.attach itself
                                logger.info(f"SUCCESSFULLY CALLED msg.attach for '{filename}' (MIME: {mimetype or 'auto-detected'}) on email Subject='{subject}'")
                            except Exception as attach_err:
                                logger.error(f"ERROR during msg.attach() for '{filename}': {attach_err}", exc_info=True)
                                # Depending on severity, you might want to stop or continue
                                # For now, we'll let it continue to try sending the email without this attachment
                        else:
                            # This case should ideally not be reached due to prior checks
                            logger.error(f"send_email_task: Attachment #{i+1} ('{filename}') - content_bytes is None unexpectedly. Skipping.")
                    else:
                        logger.warning(f"Invalid attachment_data structure for email '{subject}'. Skipping one attachment. Data: {attachment_data}")

        result = msg.send(fail_silently=False)
        logger.info(f"Email task successful for Subject='{subject}', To={to_list}. Result: {result}")
        return result

    except Exception as exc:
        logger.error(f"Email task FAILED for Subject='{subject}', To={to_list}. Error: {exc}", exc_info=True)
        try:
            retry_count = self.request.retries
            logger.warning(f"Retrying email task (attempt {retry_count + 1}/{self.max_retries})...")
            raise self.retry(exc=exc) 
        except self.MaxRetriesExceededError:
            logger.critical(f"Email task failed permanently after {self.max_retries} retries for Subject='{subject}', To={to_list}.")
            return f"Failed after {self.max_retries} retries."