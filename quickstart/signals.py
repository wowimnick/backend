# quickstart/signals.py
import logging
from django.dispatch import receiver
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Sum, Value, IntegerField
from django.db.models.functions import Coalesce

from allauth.account.signals import email_changed

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.core.cache import cache
from .models import (
    Booking,
    Reviews,
    Payment,
    Notification,
    BusinessInfo,
    CustomUser,
    ClassesMain,
    ClassOption,
    Schedule,
    ScheduleInstance,
    Payout,
)

try:
    # UTILS: Make sure this import is correct based on your structure
    from .utils.email_utils import (
        send_account_security_email,
        send_class_nearing_full_email,
        send_bulk_templated_emails,
        send_favorited_class_new_dates_email,
        send_payout_initiated_email,
    )
except ImportError:
    # Updated logging message for clarity
    logging.getLogger(__name__).error(
        "Failed to import email utility functions from .utils.email_utils."
    )

    def send_account_security_email(*args, **kwargs):
        logging.getLogger(__name__).warning("Dummy send_account_security_email called.")
        pass  # Do nothing if import fails

    def send_class_nearing_full_email(*args, **kwargs):
        logging.getLogger(__name__).warning(
            "Dummy send_class_nearing_full_email called."
        )
        pass

    def send_bulk_templated_emails(*args, **kwargs):
        logging.getLogger(__name__).warning("Dummy send_bulk_templated_emails called.")
        pass

    def send_favorited_class_new_dates_email(*args, **kwargs):
        logging.getLogger(__name__).warning(
            "Dummy send_favorited_class_new_dates_email called."
        )
        pass

    def send_payout_initiated_email(*args, **kwargs):
        logging.getLogger(__name__).warning("Dummy send_payout_initiated_email called.")
        pass


logger = logging.getLogger(__name__)
User = get_user_model()


@receiver(pre_save, sender=User)
def store_old_password(sender, instance, **kwargs):
    """
    On pre_save, if the user instance already exists in the DB,
    fetch its current password hash from the DB and store it on the instance
    so we can compare it after the save.
    """
    if instance.pk:
        try:
            # Fetch the old password hash from the database directly
            instance._old_password = User.objects.get(pk=instance.pk).password
        except User.DoesNotExist:
            instance._old_password = None


@receiver(post_save, sender=User)
def handle_user_password_change(sender, instance, created, **kwargs):
    """
    On post_save, check if the password hash has actually changed.
    This is more reliable than checking update_fields.
    """
    # We only care about existing users, not new ones
    if not created:
        # Check if the old password was stored and if it differs from the new one
        old_password = getattr(instance, "_old_password", None)
        if old_password and instance.password != old_password:
            logger.info(
                f"Password has changed for user {instance.email}. Triggering security email."
            )
            try:
                send_account_security_email(
                    instance,
                    "password",
                    subject="Your ClassEasily Password Was Changed",
                )
                logger.info(
                    f"Password change security notification prepared/queued for user {instance.email}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to trigger password change notification for {instance.email}: {e}",
                    exc_info=True,
                )


@receiver(email_changed)
def handle_email_change_signal(
    sender, request, user, from_email_address, to_email_address, **kwargs
):
    logger.info("!!! handle_email_change_signal (ALLAUTH) CALLED !!!")
    if not user:
        logger.warning("email_changed signal received without a user object.")
        return

    from_email = getattr(from_email_address, "email", "Unknown")
    to_email = getattr(to_email_address, "email", "Unknown")
    logger.info(
        f"Signal processing (ALLAUTH): Email changed for user {user.userId} from {from_email} to {to_email}"
    )
    try:
        send_account_security_email(
            user,
            "email_update",  # This ensures the correct template logic is used
            new_email=to_email,
            subject="Your ClassEasily Email Address Was Updated",
        )
        logger.info(
            f"Email change security notification prepared/queued for user {user.email}"
        )
    except Exception as e:
        logger.error(
            f"Failed to trigger email change notification for user {user.email}: {e}",
            exc_info=True,
        )


# -----------------------------------------------------------------------------------------------------------
# -------------------------------- NOTIFICATIONS ------------------------------------------------------------
# -----------------------------------------------------------------------------------------------------------


def _increment_user_unread_count(user_id):
    """Helper to increment or set the unread notification count in cache."""
    if not user_id:
        return
    cache_key_user = f"user:{user_id}:unread_notifications_count"
    try:
        # Increment, defaults to 1 if key doesn't exist (requires cache backend support like Redis/Memcached)
        # For safety, check existence first or handle the ValueError
        if cache.get(cache_key_user) is not None:
            cache.incr(cache_key_user)
        else:
            # If key doesn't exist, set it to 1. Consider fetching from DB if cache reliability is critical.
            cache.set(cache_key_user, 1, timeout=3600)  # Cache for 1 hour
        # logger.debug(f"Incremented cache key {cache_key_user}")
    except ValueError:
        # This can happen if the value isn't an integer or key doesn't exist depending on backend
        # logger.warning(f"Cache increment failed for {cache_key_user}, setting to 1.")
        cache.set(cache_key_user, 1, timeout=3600)  # Cache for 1 hour
    except Exception as e:
        logger.error(
            f"Error interacting with cache for key {cache_key_user}: {e}", exc_info=True
        )


@receiver(post_save, sender=Booking)
def create_booking_notification(sender, instance, created, **kwargs):
    """Handles in-app notifications for new and cancelled bookings."""

    # Prevent errors if related objects are missing during deletion cascade or bad data
    if not hasattr(instance, "schedule_instance") or not instance.schedule_instance:
        # logger.warning(f"Booking {instance.id} signal received without schedule_instance.")
        return
    try:
        # Navigate relationships safely
        schedule = instance.schedule_instance.schedule
        option = schedule.option
        class_main = option.classId
        business = class_main.businessId
        class_title = class_main.title  # Use the title from ClassesMain

    except (
        AttributeError,
        ClassesMain.DoesNotExist,
        BusinessInfo.DoesNotExist,
        ClassOption.DoesNotExist,
    ) as e:
        logger.error(
            f"Could not resolve related objects for booking {instance.id} notification. Error: {e}"
        )
        return

    content_type = ContentType.objects.get_for_model(instance)

    # CASE 1: New Confirmed Booking Notification for Business
    if created and instance.status == "confirmed":
        # FIXED: Removed reference to option.title, using class_title (from ClassesMain) only
        message_for_business = (
            f"New booking from {instance.user.get_full_name() or instance.user.email} "
            f"for '{class_title}' "
            f"on {instance.schedule_instance.date.strftime('%b %d')}."
        )
        link_web_for_business = f"/app/business/bookings/{instance.id}"  # Example link

        # Notify business owner
        if business.owner:
            Notification.objects.create(
                user=business.owner,
                business=business,
                notification_type="new_booking",
                message=message_for_business,
                content_type=content_type,
                object_id=instance.pk,
                icon="UserPlus",  # From your dashboard recent activity
                color="#3b82f6",  # Blue
                link_web=link_web_for_business,
            )
            _increment_user_unread_count(business.owner.pk)
            logger.info(
                f"New Booking notification created for owner {business.owner.email} for booking {instance.id}"
            )

        # Notify managers
        for manager in business.managers.all():
            # Ensure manager object and email exist, and avoid double-notifying owner
            if (
                manager
                and manager.email
                and manager.pk != getattr(business.owner, "pk", None)
            ):
                Notification.objects.create(
                    user=manager,
                    business=business,
                    notification_type="new_booking",
                    message=message_for_business,
                    content_type=content_type,
                    object_id=instance.pk,
                    icon="UserPlus",
                    color="#3b82f6",
                    link_web=link_web_for_business,
                )
                _increment_user_unread_count(manager.pk)
                logger.info(
                    f"New Booking notification created for manager {manager.email} for booking {instance.id}"
                )

    # CASE 2: Booking Cancelled Notification for Business (if cancelled by student)
    # Use update_fields to be more precise if available, check status changed to 'cancelled'
    status_changed_to_cancelled = (
        not created
        and instance.status == "cancelled"
        and (
            "status" in (kwargs.get("update_fields") or {"status"})
        )  # Check if status was part of the update
    )

    if status_changed_to_cancelled:
        # More flexible check for student-initiated cancellation
        reason_lower = (
            instance.cancellation_reason.lower() if instance.cancellation_reason else ""
        )
        # Keywords indicating student cancellation (can be adjusted)
        student_cancelled_keywords = [
            "cancelled by student",
            "student cancellation",
            "user cancelled",
            "cancelled by user",
        ]
        is_student_cancellation = any(
            keyword in reason_lower for keyword in student_cancelled_keywords
        )

        if is_student_cancellation:
            logger.info(
                f"Identified student cancellation for booking {instance.id} based on reason: '{instance.cancellation_reason}'"
            )
            # FIXED: Removed reference to option.title, using class_title (from ClassesMain) only
            message_for_business = (
                f"Booking for '{class_title}' "
                f"on {instance.schedule_instance.date.strftime('%b %d')} by {instance.user.get_full_name() or instance.user.email} was cancelled by the student."
            )
            link_web_for_business = f"/app/business/bookings/{instance.id}"

            if business.owner:
                Notification.objects.create(
                    user=business.owner,
                    business=business,
                    notification_type="booking_cancelled_by_user",
                    message=message_for_business,
                    content_type=content_type,
                    object_id=instance.pk,
                    icon="UserX",
                    color="#ef4444",
                    link_web=link_web_for_business,  # Red
                )
                _increment_user_unread_count(business.owner.pk)
                logger.info(
                    f"In-app student cancellation notification created for owner {business.owner.email} for booking {instance.id}"
                )

            for manager in business.managers.all():
                if (
                    manager
                    and manager.email
                    and manager.pk != getattr(business.owner, "pk", None)
                ):
                    Notification.objects.create(
                        user=manager,
                        business=business,
                        notification_type="booking_cancelled_by_user",
                        message=message_for_business,
                        content_type=content_type,
                        object_id=instance.pk,
                        icon="UserX",
                        color="#ef4444",
                        link_web=link_web_for_business,
                    )
                    _increment_user_unread_count(manager.pk)
                    logger.info(
                        f"In-app student cancellation notification created for manager {manager.email} for booking {instance.id}"
                    )
        # else: # Optional: Notify business if they cancelled it? Or just log
        # logger.info(f"Booking {instance.id} cancelled, but reason '{instance.cancellation_reason}' did not match student keywords for business notification.")

        # TODO: Add notification FOR THE STUDENT if the business cancels the booking.


@receiver(post_save, sender=Reviews)
def create_review_notification(sender, instance, created, **kwargs):
    """Notify business when a new review is created and approved."""
    if (
        created and instance.status == "approved"
    ):  # Or 'under_review' if you want to notify then
        try:
            business = instance.classId.businessId
            class_title = (
                instance.classId.title
            )  # Correct: Reviews links directly to ClassesMain
        except (
            AttributeError,
            ClassesMain.DoesNotExist,
            BusinessInfo.DoesNotExist,
        ) as e:
            logger.error(
                f"Could not resolve related objects for review {instance.pk} notification. Error: {e}"
            )
            return

        content_type = ContentType.objects.get_for_model(instance)
        # NOTE: instance.classId.title is correct here as Reviews links directly to ClassesMain
        message = (
            f"New {instance.rating}★ review from {instance.userId.get_full_name() or instance.userId.email} "
            f"for your class '{class_title}'."
        )
        link_web = f"/app/business/reviews/{instance.reviewId}"  # Example link

        if business.owner:
            Notification.objects.create(
                user=business.owner,
                business=business,
                notification_type="new_review",
                message=message,
                content_type=content_type,
                object_id=instance.pk,
                icon="Star",
                color="#f97316",
                link_web=link_web,  # Orange
            )
            _increment_user_unread_count(business.owner.pk)
            logger.info(
                f"New review notification created for owner {business.owner.email} for review {instance.pk}"
            )

        for manager in business.managers.all():
            if (
                manager
                and manager.email
                and manager.pk != getattr(business.owner, "pk", None)
            ):
                Notification.objects.create(
                    user=manager,
                    business=business,
                    notification_type="new_review",
                    message=message,
                    content_type=content_type,
                    object_id=instance.pk,
                    icon="Star",
                    color="#f97316",
                    link_web=link_web,
                )
                _increment_user_unread_count(manager.pk)
                logger.info(
                    f"New review notification created for manager {manager.email} for review {instance.pk}"
                )


@receiver(post_save, sender=Payment)
def create_payment_notification(sender, instance, created, **kwargs):
    """Notify business when a payment succeeds."""
    # Check if status is 'succeeded' and either it's a new record OR status was the field updated
    status_is_or_changed_to_succeeded = instance.status == "succeeded" and (
        created or "status" in (kwargs.get("update_fields") or [])
    )

    if status_is_or_changed_to_succeeded:
        try:
            booking = instance.booking
            business = booking.schedule_instance.schedule.option.classId.businessId
        except (
            AttributeError,
            Booking.DoesNotExist,
            ClassesMain.DoesNotExist,
            BusinessInfo.DoesNotExist,
            ClassOption.DoesNotExist,
        ) as e:
            logger.error(
                f"Could not resolve related objects for payment {instance.id} notification. Error: {e}"
            )
            return

        content_type = ContentType.objects.get_for_model(instance)
        message = f"Payment of ${instance.amount:.2f} received for booking ref: {booking.user_facing_reference or booking.id}."
        link_web = (
            f"/app/business/revenue"  # Or link to payment details if you have that view
        )

        if business.owner:
            Notification.objects.create(
                user=business.owner,
                business=business,
                notification_type="payment_succeeded",
                message=message,
                content_type=content_type,
                object_id=instance.pk,
                icon="DollarSign",
                color="#10b981",
                link_web=link_web,  # Green
            )
            _increment_user_unread_count(business.owner.pk)
            logger.info(
                f"Payment success notification created for owner {business.owner.email} for payment {instance.pk}"
            )

        for manager in business.managers.all():
            if (
                manager
                and manager.email
                and manager.pk != getattr(business.owner, "pk", None)
            ):
                Notification.objects.create(
                    user=manager,
                    business=business,
                    notification_type="payment_succeeded",
                    message=message,
                    content_type=content_type,
                    object_id=instance.pk,
                    icon="DollarSign",
                    color="#10b981",
                    link_web=link_web,
                )
                _increment_user_unread_count(manager.pk)
                logger.info(
                    f"Payment success notification created for manager {manager.email} for payment {instance.pk}"
                )


@receiver(post_save, sender=Reviews)
def student_review_response_notification(sender, instance, created, **kwargs):
    """Notify student when business responds to their review."""
    # Check if not created, if 'business_response' was updated, and if the response is not empty/null
    response_added_or_changed = (
        not created
        and "business_response" in (kwargs.get("update_fields") or [])
        and instance.business_response
    )

    if response_added_or_changed:
        try:
            student_user = instance.userId
            business = instance.classId.businessId
            class_title = instance.classId.title
        except (
            AttributeError,
            CustomUser.DoesNotExist,
            BusinessInfo.DoesNotExist,
            ClassesMain.DoesNotExist,
        ) as e:
            logger.error(
                f"Could not resolve related objects for review response {instance.pk} notification. Error: {e}"
            )
            return

        content_type = ContentType.objects.get_for_model(instance)
        message = (
            f"{business.businessName} responded to your review for '{class_title}'."
        )
        link_web = f"/app/user/my-reviews"  # Or to the specific class review page

        Notification.objects.create(
            user=student_user,  # Student is the recipient
            business=business,  # Store business context
            notification_type="review_response",
            message=message,
            content_type=content_type,
            object_id=instance.pk,
            icon="MessageSquare",
            color="#8b5cf6",  # Purple
            link_web=link_web,
        )
        _increment_user_unread_count(student_user.pk)
        logger.info(
            f"Review response notification created for student {student_user.email} for review {instance.reviewId}"
        )


@receiver(post_save, sender=Booking)
def check_class_capacity_notification(sender, instance: Booking, created, **kwargs):
    """
    Sends an email to the business if a class is nearing full capacity
    after a new booking is confirmed.
    """
    if instance.status != "confirmed" or not (
        "status" in (kwargs.get("update_fields") or {"status"}) or created
    ):
        return

    try:
        schedule_instance = instance.schedule_instance
        if schedule_instance.date < timezone.now().date():
            return

        confirmed_participants = Booking.objects.filter(
            schedule_instance=schedule_instance, status="confirmed"
        ).aggregate(
            total=Coalesce(Sum("participants"), Value(0), output_field=IntegerField())
        )[
            "total"
        ]

        occupancy_percentage = 0
        if schedule_instance.max_participants > 0:
            occupancy_percentage = (
                confirmed_participants / schedule_instance.max_participants
            ) * 100

        if occupancy_percentage < 80:
            return

        cache_key = (
            f"cap_notif_sent_{schedule_instance.id}_{int(occupancy_percentage/10)}"
        )
        if cache.get(cache_key):
            return

        business = schedule_instance.schedule.option.classId.businessId
        recipients = {business.owner} | set(business.managers.all())

        for recipient in recipients:
            if recipient and recipient.email:
                send_class_nearing_full_email(
                    business_user=recipient,
                    schedule_instance=schedule_instance,
                    occupancy_percentage=occupancy_percentage,
                )

        cache.set(cache_key, True, timeout=86400)
        logger.info(
            f"Class capacity email notification sent for instance {schedule_instance.id}."
        )

    except Exception as e:
        logger.error(
            f"Error in check_class_capacity_notification signal for booking {instance.id}: {e}",
            exc_info=True,
        )


@receiver(post_save, sender=Schedule)
def notify_users_of_new_schedule(sender, instance: Schedule, created, **kwargs):
    """
    After a new Schedule is created, find users who have favorited the parent
    class and send them a bulk email notification.
    """
    if not created:
        return

    try:
        is_future_schedule = False
        today = timezone.now().date()
        if instance.option.booking_type == "Full Course":
            if instance.start_date and instance.start_date >= today:
                is_future_schedule = True
        else:  # Single Session
            if instance.date and instance.date >= today:
                is_future_schedule = True

        if not is_future_schedule:
            return

        class_main = instance.option.classId
        users_who_favorited = class_main.favorited_by.all()

        if not users_who_favorited.exists():
            return

        email_data_list = [
            {
                "recipient_list": [user.email],
                "template_name": "emails/user_favorited_class_new_dates.html",
                "context": {
                    "user": user,
                    "class_main": class_main,
                    "new_schedule": instance,
                    "class_url": f"{settings.FRONTEND_BASE_URL}/classes/{class_main.slug or class_main.classId}",
                },
                "subject": f"New Dates Available for a Class You Like: {class_main.title}",
            }
            for user in users_who_favorited
            if user.email
        ]

        if email_data_list:
            send_bulk_templated_emails(email_data_list)
            logger.info(
                f"Queued {len(email_data_list)} 'favorite class new dates' emails for class {class_main.classId}."
            )

    except Exception as e:
        logger.error(
            f"Error in notify_users_of_new_schedule signal for schedule {instance.id}: {e}",
            exc_info=True,
        )


@receiver(post_save, sender=Payout)
def send_payout_notification(sender, instance: Payout, created, **kwargs):
    """
    Sends a notification to business owner/managers when a Payout record is created.
    """
    if not created:
        return

    try:
        business = instance.business
        recipients = {business.owner} | set(business.managers.all())

        for user in recipients:
            if user and user.email:
                send_payout_initiated_email(business_user=user, payout=instance)

        logger.info(f"Queued payout initiated emails for Payout ID {instance.id}")

    except Exception as e:
        logger.error(
            f"Error in send_payout_notification signal for Payout {instance.id}: {e}",
            exc_info=True,
        )
