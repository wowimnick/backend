# quickstart/signals.py
import logging
from django.dispatch import receiver
from django.conf import settings
from django.db.models.signals import post_save
from django.contrib.auth import get_user_model

from allauth.account.signals import email_changed

try:
    from .utils.email_utils import send_account_security_email
except ImportError:
    logging.error(...)
    def send_account_security_email(*args, **kwargs): ...

logger = logging.getLogger(__name__)
User = get_user_model()

@receiver(post_save, sender=User)
def handle_user_post_save_for_password(sender, instance, created, update_fields, **kwargs):
    password_potentially_changed = not created and (update_fields is None or 'password' in update_fields)
    if password_potentially_changed:
        logger.info("!!! handle_user_post_save CALLED - Password Potentially Changed !!!")
        logger.debug(f"User: {instance.email}, Created: {created}, Update Fields: {update_fields}")
        try:
            logger.info(f"Attempting to call send_account_security_email (from POST_SAVE signal) for {instance.email}")
            send_account_security_email(
                instance,
                "password",
                subject="Your ClassEasily Password Was Changed" # Pass explicit subject
            )
            logger.info(f"Password change security notification prepared/queued (from POST_SAVE signal) for user {instance.email}")
        except Exception as e:
            logger.error(f"Failed to trigger password change notification (from POST_SAVE signal) for {instance.email}: {e}", exc_info=True)

@receiver(email_changed)
def handle_email_change_signal(sender, request, user, from_email_address, to_email_address, **kwargs):
    logger.info("!!! handle_email_change_signal (ALLAUTH) CALLED !!!")
    if not user:
        logger.warning("email_changed signal received without a user object.")
        return
    logger.info(f"Signal processing (ALLAUTH): Email changed for user {user.userId} from {from_email_address.email} to {to_email_address.email}")
    try:
        logger.info(f"Attempting to call send_account_security_email (from ALLAUTH signal) for {user.email}")
        # *** FIXED change_type ***
        send_account_security_email(
            user,
            "email_update", # Changed from "email"
            new_email=to_email_address.email, # Keep passing the new email
            subject="Your ClassEasily Email Address Was Updated" # Keep explicit subject
        )
        logger.info(f"Email change security notification prepared/queued (from ALLAUTH signal) for user {user.email}")
    except Exception as e:
        logger.error(f"Failed to trigger email change notification for user {user.email}: {e}", exc_info=True)