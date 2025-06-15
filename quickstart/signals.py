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
# MODELS: Make sure these imports are correct based on your structure
from .models import Booking, Reviews, Payment, Notification, BusinessInfo, CustomUser, ClassesMain, ClassOption 

try:
    # UTILS: Make sure this import is correct based on your structure
    from .utils.email_utils import send_account_security_email
except ImportError:
     # Updated logging message for clarity
    logging.getLogger(__name__).error("Failed to import send_account_security_email from .utils.email_utils. Security emails will not be sent.")
    def send_account_security_email(*args, **kwargs):
         logging.getLogger(__name__).warning("Dummy send_account_security_email called.")
         pass # Do nothing if import fails

logger = logging.getLogger(__name__)
User = get_user_model()

@receiver(post_save, sender=User)
def handle_user_post_save_for_password(sender, instance, created, update_fields, **kwargs):
    # Check if 'password' is in update_fields, or if update_fields is None (full save) on an existing instance
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
     # Ensure email attributes exist before accessing
    from_email = getattr(from_email_address, 'email', 'Unknown')
    to_email = getattr(to_email_address, 'email', 'Unknown')
    logger.info(f"Signal processing (ALLAUTH): Email changed for user {user.userId} from {from_email} to {to_email}")
    try:
        logger.info(f"Attempting to call send_account_security_email (from ALLAUTH signal) for {user.email}")
        # *** FIXED change_type ***
        send_account_security_email(
            user,
            "email_update", # Changed from "email"
            new_email=to_email, # Keep passing the new email
            subject="Your ClassEasily Email Address Was Updated" # Keep explicit subject
        )
        logger.info(f"Email change security notification prepared/queued (from ALLAUTH signal) for user {user.email}")
    except Exception as e:
        logger.error(f"Failed to trigger email change notification for user {user.email}: {e}", exc_info=True)

# -----------------------------------------------------------------------------------------------------------
# -------------------------------- NOTIFICATIONS ------------------------------------------------------------
# -----------------------------------------------------------------------------------------------------------

def _increment_user_unread_count(user_id):
    """Helper to increment or set the unread notification count in cache."""
    if not user_id:
        return
    cache_key_user = f'user:{user_id}:unread_notifications_count'
    try:
         # Increment, defaults to 1 if key doesn't exist (requires cache backend support like Redis/Memcached)
         # For safety, check existence first or handle the ValueError
        if cache.get(cache_key_user) is not None:
             cache.incr(cache_key_user)
        else:
             # If key doesn't exist, set it to 1. Consider fetching from DB if cache reliability is critical.
             cache.set(cache_key_user, 1, timeout=3600) # Cache for 1 hour
        # logger.debug(f"Incremented cache key {cache_key_user}")
    except ValueError: 
        # This can happen if the value isn't an integer or key doesn't exist depending on backend
        # logger.warning(f"Cache increment failed for {cache_key_user}, setting to 1.")
        cache.set(cache_key_user, 1, timeout=3600) # Cache for 1 hour
    except Exception as e:
         logger.error(f"Error interacting with cache for key {cache_key_user}: {e}", exc_info=True)


@receiver(post_save, sender=Booking)
def create_booking_notification(sender, instance, created, **kwargs):
    """Handles in-app notifications for new and cancelled bookings."""
    
    # Prevent errors if related objects are missing during deletion cascade or bad data
    if not hasattr(instance, 'schedule_instance') or not instance.schedule_instance:
         # logger.warning(f"Booking {instance.id} signal received without schedule_instance.")
         return
    try:
        # Navigate relationships safely
        schedule = instance.schedule_instance.schedule
        option = schedule.option
        class_main = option.classId
        business = class_main.businessId
        class_title = class_main.title # Use the title from ClassesMain
        
    except (AttributeError, ClassesMain.DoesNotExist, BusinessInfo.DoesNotExist, ClassOption.DoesNotExist) as e:
         logger.error(f"Could not resolve related objects for booking {instance.id} notification. Error: {e}")
         return
         
    content_type = ContentType.objects.get_for_model(instance)
    
    # CASE 1: New Confirmed Booking Notification for Business
    if created and instance.status == 'confirmed':
        # FIXED: Removed reference to option.title, using class_title (from ClassesMain) only
        message_for_business = (
            f"New booking from {instance.user.get_full_name() or instance.user.email} "
            f"for '{class_title}' " 
            f"on {instance.schedule_instance.date.strftime('%b %d')}."
        )
        link_web_for_business = f"/app/business/bookings/{instance.id}" # Example link

        # Notify business owner
        if business.owner:
            Notification.objects.create(
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
            logger.info(f"New Booking notification created for owner {business.owner.email} for booking {instance.id}")

        # Notify managers
        for manager in business.managers.all():
             # Ensure manager object and email exist, and avoid double-notifying owner
            if manager and manager.email and manager.pk != getattr(business.owner, 'pk', None):
                Notification.objects.create(
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
                logger.info(f"New Booking notification created for manager {manager.email} for booking {instance.id}")
    
    # CASE 2: Booking Cancelled Notification for Business (if cancelled by student)
    # Use update_fields to be more precise if available, check status changed to 'cancelled'
    status_changed_to_cancelled = (
        not created and 
        instance.status == 'cancelled' and 
        ('status' in (kwargs.get('update_fields') or {'status'})) # Check if status was part of the update
     )
     
    if status_changed_to_cancelled:
        # More flexible check for student-initiated cancellation
        reason_lower = instance.cancellation_reason.lower() if instance.cancellation_reason else ""
        # Keywords indicating student cancellation (can be adjusted)
        student_cancelled_keywords = ["cancelled by student", "student cancellation", "user cancelled", "cancelled by user"] 
        is_student_cancellation = any(keyword in reason_lower for keyword in student_cancelled_keywords)

        if is_student_cancellation:
            logger.info(f"Identified student cancellation for booking {instance.id} based on reason: '{instance.cancellation_reason}'")
             # FIXED: Removed reference to option.title, using class_title (from ClassesMain) only
            message_for_business = (
                 f"Booking for '{class_title}' "
                 f"on {instance.schedule_instance.date.strftime('%b %d')} by {instance.user.get_full_name() or instance.user.email} was cancelled by the student."
            )
            link_web_for_business = f"/app/business/bookings/{instance.id}"

            if business.owner:
                Notification.objects.create(
                    user=business.owner, business=business, notification_type='booking_cancelled_by_user',
                    message=message_for_business, content_type=content_type, object_id=instance.pk,
                    icon="UserX", color="#ef4444", link_web=link_web_for_business # Red
                )
                _increment_user_unread_count(business.owner.pk)
                logger.info(f"In-app student cancellation notification created for owner {business.owner.email} for booking {instance.id}")
           
            for manager in business.managers.all():
                 if manager and manager.email and manager.pk != getattr(business.owner, 'pk', None):
                    Notification.objects.create(
                        user=manager, business=business, notification_type='booking_cancelled_by_user',
                        message=message_for_business, content_type=content_type, object_id=instance.pk,
                        icon="UserX", color="#ef4444", link_web=link_web_for_business
                    )
                    _increment_user_unread_count(manager.pk)
                    logger.info(f"In-app student cancellation notification created for manager {manager.email} for booking {instance.id}")
        # else: # Optional: Notify business if they cancelled it? Or just log
            # logger.info(f"Booking {instance.id} cancelled, but reason '{instance.cancellation_reason}' did not match student keywords for business notification.")
        
        # TODO: Add notification FOR THE STUDENT if the business cancels the booking.


@receiver(post_save, sender=Reviews)
def create_review_notification(sender, instance, created, **kwargs):
    """Notify business when a new review is created and approved."""
    if created and instance.status == 'approved': # Or 'under_review' if you want to notify then
        try:
             business = instance.classId.businessId
             class_title = instance.classId.title # Correct: Reviews links directly to ClassesMain
        except (AttributeError, ClassesMain.DoesNotExist, BusinessInfo.DoesNotExist) as e:
             logger.error(f"Could not resolve related objects for review {instance.pk} notification. Error: {e}")
             return

        content_type = ContentType.objects.get_for_model(instance)
        # NOTE: instance.classId.title is correct here as Reviews links directly to ClassesMain
        message = (
            f"New {instance.rating}★ review from {instance.userId.get_full_name() or instance.userId.email} "
            f"for your class '{class_title}'."
        )
        link_web = f"/app/business/reviews/{instance.reviewId}" # Example link
        
        if business.owner:
            Notification.objects.create(
                user=business.owner, business=business, notification_type='new_review',
                message=message, content_type=content_type, object_id=instance.pk,
                icon="Star", color="#f97316", link_web=link_web # Orange
            )
            _increment_user_unread_count(business.owner.pk)
            logger.info(f"New review notification created for owner {business.owner.email} for review {instance.pk}")

        for manager in business.managers.all():
             if manager and manager.email and manager.pk != getattr(business.owner, 'pk', None):
                Notification.objects.create(
                    user=manager, business=business, notification_type='new_review',
                    message=message, content_type=content_type, object_id=instance.pk,
                    icon="Star", color="#f97316", link_web=link_web
                )
                _increment_user_unread_count(manager.pk)
                logger.info(f"New review notification created for manager {manager.email} for review {instance.pk}")


@receiver(post_save, sender=Payment)
def create_payment_notification(sender, instance, created, **kwargs):
    """Notify business when a payment succeeds."""
     # Check if status is 'succeeded' and either it's a new record OR status was the field updated
    status_is_or_changed_to_succeeded = (
         instance.status == 'succeeded' and 
         (created or 'status' in (kwargs.get('update_fields') or []))
    )

    if status_is_or_changed_to_succeeded:
        try:
            booking = instance.booking
            business = booking.schedule_instance.schedule.option.classId.businessId
        except (AttributeError, Booking.DoesNotExist, ClassesMain.DoesNotExist, BusinessInfo.DoesNotExist, ClassOption.DoesNotExist) as e:
             logger.error(f"Could not resolve related objects for payment {instance.id} notification. Error: {e}")
             return
             
        content_type = ContentType.objects.get_for_model(instance)
        message = f"Payment of ${instance.amount:.2f} received for booking ref: {booking.user_facing_reference or booking.id}."
        link_web = f"/app/business/revenue" # Or link to payment details if you have that view

        if business.owner:
            Notification.objects.create(
                user=business.owner, business=business, notification_type='payment_succeeded',
                message=message, content_type=content_type, object_id=instance.pk,
                icon="DollarSign", color="#10b981", link_web=link_web # Green
            )
            _increment_user_unread_count(business.owner.pk)
            logger.info(f"Payment success notification created for owner {business.owner.email} for payment {instance.pk}")
       
        for manager in business.managers.all():
            if manager and manager.email and manager.pk != getattr(business.owner, 'pk', None):
                Notification.objects.create(
                    user=manager, business=business, notification_type='payment_succeeded',
                    message=message, content_type=content_type, object_id=instance.pk,
                    icon="DollarSign", color="#10b981", link_web=link_web
                )
                _increment_user_unread_count(manager.pk)
                logger.info(f"Payment success notification created for manager {manager.email} for payment {instance.pk}")


@receiver(post_save, sender=Reviews)
def student_review_response_notification(sender, instance, created, **kwargs):
    """Notify student when business responds to their review."""
    # Check if not created, if 'business_response' was updated, and if the response is not empty/null
    response_added_or_changed = (
        not created and 
        'business_response' in (kwargs.get('update_fields') or []) and 
        instance.business_response 
       )
       
    if response_added_or_changed:
        try:
             student_user = instance.userId
             business = instance.classId.businessId
             class_title = instance.classId.title 
        except (AttributeError, CustomUser.DoesNotExist, BusinessInfo.DoesNotExist, ClassesMain.DoesNotExist) as e:
             logger.error(f"Could not resolve related objects for review response {instance.pk} notification. Error: {e}")
             return
             
        content_type = ContentType.objects.get_for_model(instance)
        message = (
            f"{business.businessName} responded to your review for '{class_title}'."
        )
        link_web = f"/app/user/my-reviews" # Or to the specific class review page

        Notification.objects.create(
            user=student_user, # Student is the recipient
            business=business, # Store business context
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