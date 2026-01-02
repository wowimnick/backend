# quickstart/signals.py
import logging
from django.dispatch import receiver
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Sum, Value, IntegerField, Q
from django.db.models.functions import Coalesce
from django.db import transaction

from allauth.account.signals import email_changed

from django.db.models.signals import pre_save, post_save
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from django.contrib.auth.models import Permission
from django.core.cache import cache

from quickstart.tasks.business_tasks import classify_class_task
from .models import (
    Booking,
    BusinessRole,
    BusinessStaff,
    ClassCollection,
    Reviews,
    Payment,
    Notification,
    BusinessInfo,
    CustomUser,
    ClassesMain,
    ClassOption,
    Schedule,
    VerificationRequest,
    Payout,
)

logger = logging.getLogger(__name__)
User = get_user_model()


@receiver(pre_save, sender=User)
def capture_old_user_state(sender, instance, **kwargs):
    """
    Capture the state of the user before saving to detect changes in 
    sensitive fields like password or activation status.
    """
    if instance.pk:
        try:
            # Fetch the old data from the database directly
            current_db_user = User.objects.get(pk=instance.pk)
            instance._old_password = current_db_user.password
            instance._old_is_active = current_db_user.is_active
        except User.DoesNotExist:
            instance._old_password = None
            instance._old_is_active = None
    else:
        # User is being created
        instance._old_password = None
        instance._old_is_active = None


@receiver(post_save, sender=User)
def handle_user_password_change(sender, instance, created, **kwargs):
    from .utils.email_utils import send_account_security_email
    
    if created:
        return

    # 1. Optimization: If update_fields was used and 'password' is not in it, 
    # the password definitely didn't change (DB-wise).
    # This prevents signals from firing on simple updates like 'last_login'.
    if kwargs.get('update_fields') and 'password' not in kwargs['update_fields']:
        return

    # 2. Retrieve captured old state
    old_password = getattr(instance, "_old_password", None)
    old_is_active = getattr(instance, "_old_is_active", None)

    # 3. Check for Activation (False -> True)
    # If the user is transitioning from inactive to active (e.g., verifying email),
    # we suppress the password change notification. This avoids false positives
    # during the verification flow where the user model is saved.
    is_activating = (old_is_active is False) and (instance.is_active is True)
    
    if is_activating:
        logger.info(f"User {instance.email} is being activated. Skipping password change notification.")
        return

    # 4. Check actual password change
    # Only verify if we have a valid old password to compare against and it differs
    if old_password is not None and instance.password != old_password:
        logger.info(f"Password has changed for user {instance.email}. Triggering security email.")
        try:
            send_account_security_email(
                instance,
                "password",
                subject="Your ClassEasily Password Was Changed",
            )
            logger.info(f"Password change notification queued for {instance.email}")
        except Exception as e:
            logger.error(f"Failed to trigger password notification for {instance.email}: {e}", exc_info=True)


@receiver(email_changed)
def handle_email_change_signal(sender, request, user, from_email_address, to_email_address, **kwargs):
    from .utils.email_utils import send_account_security_email

    logger.info("!!! handle_email_change_signal (ALLAUTH) CALLED !!!")
    if not user:
        return

    from_email = getattr(from_email_address, "email", "Unknown")
    to_email = getattr(to_email_address, "email", "Unknown")
    
    try:
        send_account_security_email(
            user,
            "email_update",
            new_email=to_email,
            subject="Your ClassEasily Email Address Was Updated",
        )
        logger.info(f"Email change notification queued for {user.email}")
    except Exception as e:
        logger.error(f"Failed to trigger email notification for {user.email}: {e}", exc_info=True)


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

    # --- FIX: Determine Booker Name safely (User or Guest Contact) ---
    booker_name = "Unknown Guest"
    if instance.user:
        booker_name = instance.user.get_full_name() or instance.user.email
    elif instance.contact:
        # Construct name from contact
        name_parts = [
            n for n in [instance.contact.first_name, instance.contact.last_name] if n
        ]
        booker_name = " ".join(name_parts) if name_parts else instance.contact.email
    # -----------------------------------------------------------------

    if created and instance.status == "confirmed" and business.newBookingNotification:
        message_for_business = (
            f"New booking from {booker_name} "
            f"for '{class_title}' "
            f"on {instance.schedule_instance.date.strftime('%b %d')}."
        )
        link_web_for_business = f"/business/dashboard/bookings"  # Example link

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
            "cancelled by guest", # Added this keyword for guest cancellation
        ]
        is_student_cancellation = any(
            keyword in reason_lower for keyword in student_cancelled_keywords
        )

        # CRITICAL UPDATE: Checks business.cancellationNotification toggle
        if is_student_cancellation and business.cancellationNotification:
            logger.info(
                f"Identified student cancellation for booking {instance.id} based on reason: '{instance.cancellation_reason}'"
            )
            # FIXED: Use safe booker_name variable
            message_for_business = (
                f"Booking for '{class_title}' "
                f"on {instance.schedule_instance.date.strftime('%b %d')} by {booker_name} was cancelled by the student."
            )
            link_web_for_business = f"/business/dashboard/bookings" 

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
        link_web = f"/business/dashboard/reviews"

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
        link_web = f"/business/dashboard/trends" 

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
        link_web = f"/my-classes?tab=completed"

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

@receiver(post_save, sender=Schedule)
def notify_users_of_new_schedule(sender, instance: Schedule, created, **kwargs):
    """
    After a new Schedule is created, find users who have favorited the parent
    class and send them a bulk email notification.

    Includes debouncing to prevent spamming users when multiple schedules
    are added in a batch (e.g. 20 weeks of Monday classes).
    """
    from .utils.email_utils import send_favorited_class_new_dates_email

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

        sent_count = 0
        skipped_count = 0

        for user in users_who_favorited:
            if user.email:
                # --- DEBOUNCING LOGIC ---
                # Cache key unique to User + Class combination
                cache_key = (
                    f"fav_new_dates_sent_{user.userId}_{class_main.classId}"
                )

                # Check if we already sent an email for this class to this user recently
                if cache.get(cache_key):
                    skipped_count += 1
                    continue

                # If not, send the email and set the cache
                send_favorited_class_new_dates_email(
                    user=user, class_main=class_main, new_schedule=instance
                )
                
                # Set cache to prevent another email for 2 hours (7200 seconds)
                # This assumes that batch uploads happen within this window.
                cache.set(cache_key, True, timeout=7200) 
                sent_count += 1

        if sent_count > 0:
            logger.info(
                f"Queued {sent_count} 'favorite class new dates' emails for class {class_main.classId}. "
                f"Skipped {skipped_count} due to debouncing."
            )

    except Exception as e:
        logger.error(
            f"Error in notify_users_of_new_schedule signal for schedule {instance.id}: {e}",
            exc_info=True,
        )

@receiver(post_save, sender=ClassesMain)
def trigger_classification(sender, instance, created, update_fields, **kwargs):
    """
    Triggers the auto-assignment logic when a class is created or relevant fields change.
    """
    should_run = False
    
    if created:
        should_run = True
    elif update_fields:
        # Only run if fields relevant to rules have changed
        relevant_fields = {'title', 'description', 'status', 'category'}
        if any(field in update_fields for field in relevant_fields):
            should_run = True
    else:
        should_run = True

    if should_run and instance.status == 'active':
        # FIX: Wrap in on_commit to prevent race conditions where the worker
        # executes before the DB transaction is finalized.
        transaction.on_commit(
            lambda: classify_class_task.delay(instance.pk)
        )

@receiver(post_save, sender=ClassCollection)
def trigger_reclassification_on_collection_change(sender, instance, created, update_fields, **kwargs):
    """
    If an Automated Collection is created or its rules change, we must 
    re-evaluate ALL active classes to see if they now fit this new collection.
    """
    # 1. Quick check: Is this an automated collection?
    if instance.type != 'automated':
        return

    # 2. Determine if we should run. 
    # If created, yes. 
    # If updated, check if 'automation_rules' or 'is_active' changed.
    should_run = False
    if created:
        should_run = True
    elif update_fields:
        relevant_fields = {'automation_rules', 'type', 'is_active'}
        if any(field in update_fields for field in relevant_fields):
            should_run = True
    else:
        # Full save without specific update_fields usually implies a form save
        should_run = True

    if should_run:
        # 3. Fetch all active classes
        # NOTE: This triggers 1 LLM call per active class. 
        # If you have 5,000 classes, this queues 5,000 tasks ($$$).
        active_class_ids = ClassesMain.objects.filter(status='active').values_list('pk', flat=True)
        
        logger.info(f"🔄 Collection '{instance.name}' changed. Re-queueing {len(active_class_ids)} classes for classification.")

        # 4. Queue tasks safely using on_commit
        def queue_bulk_tasks():
            for class_id in active_class_ids:
                classify_class_task.delay(class_id)

        transaction.on_commit(queue_bulk_tasks)

@receiver(post_save, sender=Payout)
def send_payout_notification(sender, instance: Payout, created, **kwargs):
    """
    Sends a notification to business owner/managers when a Payout record is created.
    """
    if not created:
        return
    
    from .utils.email_utils import send_payout_initiated_email 

    try:
        business = instance.business
        # Collect all unique recipients (Owner + Managers)
        recipients = {business.owner} | set(business.managers.all())

        for user in recipients:
            if user and user.email:
                # FIX: Queue EACH email to send only after the transaction commits successfully.
                # We use (u=user) to bind the variable correctly in the lambda loop.
                transaction.on_commit(
                    lambda u=user: send_payout_initiated_email(business_user=u, payout=instance)
                )
                
        logger.info(f"Queued payout initiated emails for Payout ID {instance.id} (waiting for DB commit)")

    except Exception as e:
        logger.error(
            f"Error in send_payout_notification signal for Payout {instance.id}: {e}",
            exc_info=True,
        )


@receiver(post_save, sender=VerificationRequest)
def notify_admins_on_new_verification(sender, instance, created, **kwargs):
    """
    Sends a notification to admins when a new verification request is created.
    """
    from .utils.email_utils import send_admin_new_verification_request_email

    # Only run this logic when a VerificationRequest is first created.
    if not created:
        return

    logger.info(
        f"Signal 'notify_admins_on_new_verification' triggered for VerificationRequest ID: {instance.id}"
    )

    try:
        # This logic is copied directly from your verification_views.py
        content_type = ContentType.objects.get_for_model(VerificationRequest)
        admin_perm_codename = "process_verificationrequest"
        admin_perm = Permission.objects.get(
            content_type=content_type, codename=admin_perm_codename
        )

        admin_users = (
            User.objects.filter(
                Q(is_superuser=True)
                | Q(groups__permissions=admin_perm)
                | Q(user_permissions=admin_perm)
            )
            .filter(is_active=True, email__isnull=False)
            .exclude(email="")
            .distinct()
        )
        admin_emails = list(admin_users.values_list("email", flat=True))

        if admin_emails:
            # Call the email utility function
            send_admin_new_verification_request_email(admin_emails, instance)
            logger.info(
                f"Admin notification queued via SIGNAL for new verification request {instance.id} to {len(admin_emails)} admins."
            )
        else:
            logger.warning(
                f"SIGNAL: No active admin users found with '{admin_perm_codename}' permission to notify about verification {instance.id}"
            )

    except Permission.DoesNotExist:
        logger.error(
            f"SIGNAL CRITICAL: Permission '{admin_perm_codename}' not found. Cannot notify admins about new verification request."
        )
    except Exception as e:
        logger.error(
            f"SIGNAL ERROR: Failed to send admin notification email for new verification {instance.id}: {e}",
            exc_info=True,
        )


@receiver(post_save, sender=BusinessInfo)
def create_default_business_role(sender, instance, created, **kwargs):
    """
    When a new business is created, automatically create a default 'Owner' role
    for them with all available business-level permissions.
    """
    if created:
        # List of all codenames for permissions a business owner can assign
        business_permission_codenames = [
            "access_business_dashboard",
            "manage_own_classes",
            "manage_own_schedule_instances",
            "view_own_business_bookings",
            "manage_own_business_profile",
            "manage_business_staff",
            "manage_business_roles",
            "view_business_revenue_analytics",
            "export_business_revenue_data",
            "view_business_students",
            "add_studentnote",
            "view_studentnote",
            "view_own_booking_analytics",
            "cancel_business_booking",
            "view_own_business_reviews",
            "add_business_review_response",
            "manage_own_business_discounts",
            "receive_booking_notifications",
        ]

        # Fetch all the relevant permission objects in one query
        permissions = Permission.objects.filter(
            codename__in=business_permission_codenames
        )

        # Create the new BusinessRole
        owner_role = BusinessRole.objects.create(
            business=instance,
            name="Business Owner",
            description="Full access to manage this business.",
        )

        # Assign all the permissions to this new role
        owner_role.permissions.set(permissions)

        # Automatically assign the business owner to this role
        BusinessStaff.objects.create(
            business=instance,
            user=instance.owner,
            role=owner_role,
            status="accepted",  # The owner is automatically accepted
            invited_email=instance.owner.email,
            invited_by=instance.owner,
        )