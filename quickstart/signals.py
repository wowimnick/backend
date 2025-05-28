# quickstart/signals.py
import logging
from django.dispatch import receiver
from django.conf import settings
from django.db.models.signals import post_save
from django.contrib.auth import get_user_model

from allauth.account.signals import email_changed

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from .models import Booking, Reviews, Payment, Notification, BusinessInfo, CustomUser

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

# -----------------------------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------------------------
# -----------------------------------------------------------------------------------------------------------

def _increment_user_unread_count(user_id):
    cache_key_user = f'user:{user_id}:unread_notifications_count'
    try:
        cache.incr(cache_key_user)
    except ValueError: # Key might not exist
        cache.set(cache_key_user, 1, timeout=3600) # Cache for 1 hour

@receiver(post_save, sender=Booking)
def create_booking_notification(sender, instance, created, **kwargs):
    if created and instance.status == 'confirmed':
        business = instance.schedule_instance.schedule.option.classId.businessId
        content_type = ContentType.objects.get_for_model(instance)
        
        message_for_business = (
            f"New booking from {instance.user.first_name} {instance.user.last_name} "
            f"for {instance.schedule_instance.schedule.option.classId.title} - {instance.schedule_instance.schedule.option.title} "
            f"on {instance.schedule_instance.date.strftime('%b %d')}."
        )
        link_web_for_business = f"/app/business/bookings/{instance.id}" # Example link

        # Notify business owner
        if business.owner:
            notif_owner = Notification.objects.create(
                user=business.owner,
                business=business,
                notification_type='new_booking',
                message=message_for_business,
                content_type=content_type,
                object_id=instance.pk,
                icon="UserPlus", # From your dashboard recent activity
                color="#3b82f6", # Blue
                link_web=link_web_for_business
            )
            _increment_user_unread_count(business.owner.pk)
            logger.info(f"Booking notification created for owner {business.owner.email} for booking {instance.id}")

        # Notify managers
        for manager in business.managers.all():
            if manager != business.owner: # Avoid double notifying if owner is also a manager
                notif_manager = Notification.objects.create(
                    user=manager,
                    business=business,
                    notification_type='new_booking',
                    message=message_for_business,
                    content_type=content_type,
                    object_id=instance.pk,
                    icon="UserPlus",
                    color="#3b82f6",
                    link_web=link_web_for_business
                )
                _increment_user_unread_count(manager.pk)
                logger.info(f"Booking notification created for manager {manager.email} for booking {instance.id}")
    
    elif not created and instance.status == 'cancelled':
        business = instance.schedule_instance.schedule.option.classId.businessId
        content_type = ContentType.objects.get_for_model(instance)
        
        # More flexible check for student-initiated cancellation
        # Convert both to lowercase for case-insensitive comparison
        reason_lower = instance.cancellation_reason.lower() if instance.cancellation_reason else ""
        
        # Check for keywords indicating student cancellation
        student_cancelled_keywords = ["cancelled by student", "student cancellation", "user cancelled"] 
        
        is_student_cancellation = any(keyword in reason_lower for keyword in student_cancelled_keywords)

        if is_student_cancellation:
            logger.info(f"Identified student cancellation for booking {instance.id} based on reason: '{instance.cancellation_reason}'") # Add log
            message_for_business = (
                f"Booking for {instance.schedule_instance.schedule.option.classId.title} - {instance.schedule_instance.schedule.option.title} "
                f"on {instance.schedule_instance.date.strftime('%b %d')} by {instance.user.first_name} was cancelled by the student." # Message can be more generic
            )
            link_web_for_business = f"/app/business/bookings/{instance.id}"

            if business.owner:
                Notification.objects.create(
                    user=business.owner, business=business, notification_type='booking_cancelled_by_user',
                    message=message_for_business, content_type=content_type, object_id=instance.pk,
                    icon="UserX", color="#ef4444", link_web=link_web_for_business # Red
                )
                _increment_user_unread_count(business.owner.pk)
                logger.info(f"In-app notification for student cancellation created for owner {business.owner.email}")
            for manager in business.managers.all():
                if manager != business.owner:
                    Notification.objects.create(
                        user=manager, business=business, notification_type='booking_cancelled_by_user',
                        message=message_for_business, content_type=content_type, object_id=instance.pk,
                        icon="UserX", color="#ef4444", link_web=link_web_for_business
                    )
                    _increment_user_unread_count(manager.pk)
                    logger.info(f"In-app notification for student cancellation created for manager {manager.email}")
        else:
            logger.info(f"Booking {instance.id} cancelled, but reason '{instance.cancellation_reason}' did not match student cancellation keywords for in-app notification.")

@receiver(post_save, sender=Reviews)
def create_review_notification(sender, instance, created, **kwargs):
    if created and instance.status == 'approved': # Or 'under_review' if you want to notify then
        business = instance.classId.businessId
        content_type = ContentType.objects.get_for_model(instance)
        message = (
            f"New {instance.rating}★ review from {instance.userId.first_name} "
            f"for your class '{instance.classId.title}'."
        )
        link_web = f"/app/business/reviews/{instance.reviewId}" # Example link
        
        if business.owner:
            Notification.objects.create(
                user=business.owner, business=business, notification_type='new_review',
                message=message, content_type=content_type, object_id=instance.pk,
                icon="Star", color="#f97316", link_web=link_web # Orange
            )
            _increment_user_unread_count(business.owner.pk)
        for manager in business.managers.all():
             if manager != business.owner:
                Notification.objects.create(
                    user=manager, business=business, notification_type='new_review',
                    message=message, content_type=content_type, object_id=instance.pk,
                    icon="Star", color="#f97316", link_web=link_web
                )
                _increment_user_unread_count(manager.pk)

@receiver(post_save, sender=Payment)
def create_payment_notification(sender, instance, created, **kwargs):
    # Only send if it's a new successful payment, or status changes to succeeded
    if instance.status == 'succeeded' and (created or kwargs.get('update_fields') and 'status' in kwargs.get('update_fields')):
        booking = instance.booking
        business = booking.schedule_instance.schedule.option.classId.businessId
        content_type = ContentType.objects.get_for_model(instance)
        message = f"Payment of ${instance.amount:.2f} received for booking #{booking.id}."
        link_web = f"/app/business/revenue" # Or link to payment details if you have that view

        if business.owner:
            Notification.objects.create(
                user=business.owner, business=business, notification_type='payment_succeeded',
                message=message, content_type=content_type, object_id=instance.pk,
                icon="DollarSign", color="#10b981", link_web=link_web # Green
            )
            _increment_user_unread_count(business.owner.pk)
        for manager in business.managers.all():
            if manager != business.owner:
                Notification.objects.create(
                    user=manager, business=business, notification_type='payment_succeeded',
                    message=message, content_type=content_type, object_id=instance.pk,
                    icon="DollarSign", color="#10b981", link_web=link_web
                )
                _increment_user_unread_count(manager.pk)

# Similar signal for when business responds to a review, to notify the student:
@receiver(post_save, sender=Reviews)
def student_review_response_notification(sender, instance, created, **kwargs):
    if not created and 'business_response' in (kwargs.get('update_fields') or []) and instance.business_response:
        student_user = instance.userId
        business = instance.classId.businessId
        content_type = ContentType.objects.get_for_model(instance)
        message = (
            f"{business.businessName} responded to your review for '{instance.classId.title}'."
        )
        # Link for student to view their reviews/the specific review
        link_web = f"/app/user/my-reviews" # Or to the specific class review page

        Notification.objects.create(
            user=student_user, # Student is the recipient
            # business can be null here or still point to the business for context
            business=business, 
            notification_type='review_response',
            message=message,
            content_type=content_type,
            object_id=instance.pk,
            icon="MessageSquare", 
            color="#8b5cf6", # Purple
            link_web=link_web
        )
        _increment_user_unread_count(student_user.pk)
        logger.info(f"Review response notification created for student {student_user.email} for review {instance.reviewId}")
