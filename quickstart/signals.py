# quickstart/signals.py
import logging
from django.dispatch import receiver
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Sum, Value, IntegerField, Q
from django.db.models.functions import Coalesce
from django.db import transaction

from allauth.account.signals import email_changed

from django.db.models.signals import pre_save, post_save, post_delete
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.contrib.postgres.search import SearchVector

from django.contrib.auth.models import Permission
from django.core.cache import cache

logger = logging.getLogger(__name__)

from quickstart.utils.sms_utils import business_sms_enabled
# Do NOT import quickstart.tasks here: it pulls in the whole tasks package (cache_tasks → cache_prewarm → views → pandas/boto3) and can add 5+ minutes to Django startup. Use send_task by name in handlers instead.
from .models import (
    Booking,
    BusinessRole,
    BusinessStaff,
    ClassCollection,
    ClassCategory,
    ClassSubcategory,
    Contact,
    Reviews,
    Payment,
    Notification,
    BusinessInfo,
    CustomUser,
    BlogPost,
    ClassesMain,
    ClassOption,
    ImportedGoogleReview,
    Schedule,
    ScheduleInstance,
    StudentNote,
    VerificationRequest,
    Payout,
)

def _invalidate_public_class_search_preset_cache(
    affected_locations=None,
    affected_collection_slugs=None,
    class_main=None,
):
    """Invalidate and repopulate public class search cache (class/schedule/instance/collection change).
    Delegates to view so prewarm runs with new version before bump — users always get cached.
    Pass affected_* to prewarm only what changed; or pass class_main (ClassesMain) to derive collections."""
    try:
        from quickstart.views.public.public_class_views import (
            invalidate_public_class_search_preset_cache,
        )
        if class_main is not None:
            try:
                slugs = list(class_main.collections.values_list("slug", flat=True))
                affected_collection_slugs = slugs if slugs else affected_collection_slugs
            except Exception:
                pass
            if affected_collection_slugs is None and affected_locations is None:
                invalidate_public_class_search_preset_cache()
            else:
                invalidate_public_class_search_preset_cache(
                    affected_locations=affected_locations,
                    affected_collection_slugs=affected_collection_slugs,
                )
        else:
            invalidate_public_class_search_preset_cache(
                affected_locations=affected_locations,
                affected_collection_slugs=affected_collection_slugs,
            )
    except Exception as e:
        logger.warning("Failed to invalidate public class search preset cache: %s", e)


User = get_user_model()

@receiver(email_changed)
def handle_email_change_signal(sender, request, user, from_email_address, to_email_address, **kwargs):
    from .utils.email_utils import send_account_security_email

    logger.info("!!! handle_email_change_signal (ALLAUTH) CALLED !!!")
    if not user:
        return

    from_email = getattr(from_email_address, "email", "Unknown")
    to_email = getattr(to_email_address, "email", "Unknown")
    
    try:
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
        link_web_for_business = (
            f"/business/dashboard/bookings/active?bookingId={instance.pk}"
        )

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

    # Super Admin new-booking email is NOT sent here.
    # It is sent only when payment is completed (or booking confirmed for free/GC),
    # from the payment flow, at the same time the business receives its email.

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
            link_web_for_business = (
                f"/business/dashboard/bookings/active?bookingId={instance.pk}"
            )

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

            if business_sms_enabled(business):
                try:
                    from quickstart.utils.sms_utils import normalize_phone_for_sns
                    from quickstart.tasks.notification_tasks import send_sms_task
                    date_str = instance.schedule_instance.date.strftime("%b %d") if instance.schedule_instance and instance.schedule_instance.date else ""
                    t = instance.schedule_instance.time if instance.schedule_instance else None
                    time_str = t.strftime("%I:%M %p").lstrip("0") if t and hasattr(t, "strftime") else (str(t) if t else "")
                    when_str = f"on {date_str} at {time_str}" if time_str else f"on {date_str}"
                    sms_msg = f"A booking was cancelled: {class_title} {when_str}.\n\nSpot is available again.\n\n— ClassEasily"
                    if business.owner:
                        normalized = normalize_phone_for_sns(getattr(business.owner, "phone_number", None) or "")
                        if normalized:
                            send_sms_task.delay(normalized, sms_msg)
                    for manager in business.managers.all():
                        if manager and manager.pk != getattr(business.owner, "pk", None):
                            normalized = normalize_phone_for_sns(getattr(manager, "phone_number", None) or "")
                            if normalized:
                                send_sms_task.delay(normalized, sms_msg)
                except Exception as sms_e:
                    logger.warning("Cancellation SMS to business failed for booking %s: %s", instance.id, sms_e)

        # Super Admin email: booking cancelled (any cancellation)
        try:
            from .utils.email_utils import send_super_admin_booking_cancelled_email
            send_super_admin_booking_cancelled_email(instance)
        except Exception as e:
            logger.error(
                f"Failed to send Super Admin booking-cancelled email for booking {instance.id}: {e}",
                exc_info=True,
            )


@receiver(post_save, sender=Booking)
def update_business_last_booking_date(sender, instance, **kwargs):
    """Maintain BusinessInfo.last_booking_date as newest booking.booking_date for KPIs/backfill."""
    if not getattr(instance, "booking_date", None):
        return
    try:
        class_main = instance.schedule_instance.schedule.option.classId
        biz_pk = class_main.businessId_id
    except AttributeError:
        return
    try:
        existing = BusinessInfo.objects.filter(pk=biz_pk).values_list(
            "last_booking_date", flat=True
        ).first()
        if existing is None or instance.booking_date > existing:
            BusinessInfo.objects.filter(pk=biz_pk).update(
                last_booking_date=instance.booking_date
            )
    except Exception as e:
        logger.warning(
            "Could not update last_booking_date for business %s booking %s: %s",
            biz_pk,
            getattr(instance, "pk", None),
            e,
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
        link_web = f"/business/dashboard/reviews?reviewId={instance.pk}"

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
        link_web = f"/business/dashboard/revenue?paymentId={instance.pk}"

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
        booking_pk = getattr(instance, "booking_id", None)
        link_web = (
            f"/my-classes?tab=completed&highlight={booking_pk}"
            if booking_pk
            else "/my-classes?tab=completed"
        )

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


def _bust_homepage_sections_cache():
    """Invalidate short-lived homepage row cache (trending / date_night / next_week)."""
    key = f"homepage_sections_v1:{getattr(settings, 'DJANGO_ENV', 'local')}"
    try:
        cache.delete(key)
    except Exception as e:
        logger.warning("homepage sections cache bust failed: %s", e)


@receiver(post_save, sender=Reviews)
@receiver(post_delete, sender=Reviews)
def sync_denorm_platform_reviews(sender, instance, **kwargs):
    from quickstart.utils.review_denorm import refresh_platform_review_aggregates_for_class

    cid = getattr(instance, "classId_id", None)
    if cid:
        try:
            refresh_platform_review_aggregates_for_class(cid)
        except Exception as e:
            logger.warning(
                "refresh platform review denorm failed: %s", e, exc_info=True
            )
    _bust_homepage_sections_cache()


@receiver(post_save, sender=ImportedGoogleReview)
@receiver(post_delete, sender=ImportedGoogleReview)
def sync_denorm_google_reviews(sender, instance, **kwargs):
    from quickstart.utils.review_denorm import refresh_google_review_aggregates_for_business

    bid = getattr(instance, "business_id", None)
    if bid:
        try:
            refresh_google_review_aggregates_for_business(bid)
        except Exception as e:
            logger.warning(
                "refresh google review denorm failed: %s", e, exc_info=True
            )
    _bust_homepage_sections_cache()


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
    try:
        class_main = instance.option.classId if getattr(instance, "option", None) else None
        _invalidate_public_class_search_preset_cache(class_main=class_main)
    except Exception:
        _invalidate_public_class_search_preset_cache()


@receiver(post_delete, sender=Schedule)
def schedule_post_delete_invalidate_search_cache(sender, instance, **kwargs):
    try:
        class_main = instance.option.classId if getattr(instance, "option", None) else None
        _invalidate_public_class_search_preset_cache(class_main=class_main)
    except Exception:
        _invalidate_public_class_search_preset_cache()


@receiver(post_save, sender=ScheduleInstance)
@receiver(post_delete, sender=ScheduleInstance)
def schedule_instance_change_invalidate_search_cache(sender, instance, **kwargs):
    try:
        s = getattr(instance, "schedule", None)
        class_main = s.option.classId if s and getattr(s, "option", None) else None
        _invalidate_public_class_search_preset_cache(class_main=class_main)
    except Exception:
        _invalidate_public_class_search_preset_cache()


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
        relevant_fields = {'title', 'description', 'status'}
        if any(field in update_fields for field in relevant_fields):
            should_run = True
    else:
        should_run = True

    if should_run and instance.status == 'active':
        # FIX: Wrap in on_commit to prevent race conditions where the worker
        # executes before the DB transaction is finalized.
        class_pk = instance.pk
        def _queue_classify():
            from CEBackend.celery import app as celery_app
            celery_app.send_task(
                "quickstart.tasks.business_tasks.classify_class_task",
                args=[class_pk],
            )
        transaction.on_commit(_queue_classify)

@receiver(pre_save, sender=ClassesMain)
def classes_main_track_description_before(sender, instance, **kwargs):
    if not instance.pk:
        instance._description_before = None
        return
    try:
        instance._description_before = ClassesMain.objects.only("description").get(
            pk=instance.pk
        ).description
    except ClassesMain.DoesNotExist:
        instance._description_before = None


@receiver(post_save, sender=ClassesMain)
def queue_description_ai_formatting(sender, instance, created, **kwargs):
    """Queue Gemini description structuring when description text changes."""
    if instance.status != "active":
        return
    prev = getattr(instance, "_description_before", object())
    desc = instance.description or ""
    if not created:
        if prev is not object() and prev == desc:
            return
    elif not (desc or "").strip():
        return

    class_pk = instance.pk
    ClassesMain.objects.filter(pk=class_pk).update(description_ai_status="pending")

    def _queue():
        from CEBackend.celery import app as celery_app

        celery_app.send_task(
            "quickstart.tasks.business_tasks.format_class_description_task",
            args=[class_pk],
        )

    transaction.on_commit(_queue)


@receiver(post_save, sender=Payout)
def send_payout_notification(sender, instance: Payout, created, **kwargs):
    """
    Sends a notification to business owner/managers when a Payout record is created.
    Skipped when metadata contains skip_payout_notification (e.g. DB backfill of an existing Stripe transfer).
    """
    if not created:
        return

    meta = instance.metadata if isinstance(instance.metadata, dict) else {}
    if meta.get("skip_payout_notification"):
        return

    from .utils.email_utils import send_payout_initiated_email 

    try:
        business = instance.business
        # Collect all unique recipients (Owner + Managers)
        recipients = {business.owner} | set(business.managers.all())

        recipient_list = [u for u in recipients if u and getattr(u, "email", None)]

        for user in recipient_list:
            # FIX: Queue EACH email to send only after the transaction commits successfully.
            # We use (u=user) to bind the variable correctly in the lambda loop.
            transaction.on_commit(
                lambda u=user: send_payout_initiated_email(business_user=u, payout=instance)
            )

        def _create_payout_in_app_notifications():
            from quickstart.utils.notification_utils import create_notifications_for_users

            amt = f"{instance.amount:.2f}"
            cur = (instance.currency or "").upper() or "CAD"
            msg = f"A payout of {amt} {cur} has been initiated to your account."
            create_notifications_for_users(
                recipient_list,
                "payout_initiated",
                msg,
                "DollarSign",
                "#22c55e",
                "/business/dashboard?tab=payouts",
                business=business,
                object_id=str(instance.pk),
            )

        transaction.on_commit(_create_payout_in_app_notifications)

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
            "manage_email_marketing",
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


# ---------------------------------------------------------------------------
# Search vector and contact/booking signals (moved from models.py)
# ---------------------------------------------------------------------------


def get_classesmain_search_vector(instance):
    """Build the search vector for a ClassesMain instance (for full-text search)."""
    from django.db.models import Value

    vector_components = [
        SearchVector(Value(instance.title), weight="A", config="english"),
        SearchVector(Value(instance.description), weight="B", config="english"),
    ]
    if instance.businessId:
        vector_components.append(
            SearchVector(
                Value(instance.businessId.businessName), weight="B", config="english"
            )
        )
    if not vector_components:
        return SearchVector(Value(""))
    final_vector = vector_components[0]
    for component in vector_components[1:]:
        final_vector += component
    return final_vector


@receiver(post_save, sender=ClassesMain)
def classesmain_post_save_receiver(sender, instance, created, update_fields, **kwargs):
    if kwargs.get("raw", False):
        return
    should_update = created
    if not created and update_fields:
        text_fields = {"title", "description"}
        if any(f in update_fields for f in text_fields):
            should_update = True
    elif not created and update_fields is None:
        should_update = True
    if should_update:
        new_vector = get_classesmain_search_vector(instance)
        if instance.search_vector != new_vector:
            ClassesMain.objects.filter(pk=instance.pk).update(search_vector=new_vector)
    _invalidate_public_class_search_preset_cache(class_main=instance)


@receiver(post_delete, sender=ClassesMain)
def classesmain_post_delete_invalidate_search_cache(sender, instance, **kwargs):
    _invalidate_public_class_search_preset_cache(class_main=instance)


@receiver(post_save, sender="quickstart.ClassOption")
@receiver(post_delete, sender="quickstart.ClassOption")
def classoption_change_receiver(sender, instance, **kwargs):
    if hasattr(instance, "classId") and instance.classId:
        class_instance = instance.classId
        new_vector = get_classesmain_search_vector(class_instance)
        if class_instance.search_vector != new_vector:
            ClassesMain.objects.filter(pk=class_instance.pk).update(
                search_vector=new_vector
            )
        _invalidate_public_class_search_preset_cache(class_main=class_instance)
    else:
        _invalidate_public_class_search_preset_cache()


@receiver(post_save, sender="quickstart.ClassCategory")
def classcategory_change_receiver(sender, instance, update_fields, **kwargs):
    # ClassesMain no longer has category FK; no classes to reindex.
    pass


@receiver(post_save, sender="quickstart.ClassSubcategory")
def classsubcategory_change_receiver(sender, instance, update_fields, **kwargs):
    # ClassesMain no longer has subcategory FK; no classes to reindex.
    pass


@receiver(post_save, sender=CustomUser)
def link_contact_on_user_creation(sender, instance, created, **kwargs):
    """After a new user registers, link them to an existing CRM contact if one exists."""
    if created and instance.email:
        try:
            with transaction.atomic():
                contact_to_link = Contact.objects.select_for_update().get(
                    email__iexact=instance.email, user__isnull=True
                )
                contact_to_link.user = instance
                if instance.first_name and not contact_to_link.first_name:
                    contact_to_link.first_name = instance.first_name
                if instance.last_name and not contact_to_link.last_name:
                    contact_to_link.last_name = instance.last_name
                if instance.phone_number and not contact_to_link.phone_number:
                    contact_to_link.phone_number = instance.phone_number
                contact_to_link.save()
                logger.info(
                    f"Successfully linked new user {instance.email} to existing CRM Contact ID {contact_to_link.id}."
                )
        except Contact.DoesNotExist:
            pass
        except Exception as e:
            logger.error(
                f"Error linking new user {instance.email} to a CRM contact: {e}",
                exc_info=True,
            )


@receiver(post_delete, sender="quickstart.Contact")
def delete_contact_notes(sender, instance, **kwargs):
    """When a Contact is deleted, delete any StudentNote records that point to it."""
    content_type = ContentType.objects.get_for_model(instance)
    StudentNote.objects.filter(
        content_type=content_type, object_id=instance.pk
    ).delete()
    logger.info(f"Deleted all notes associated with Contact ID {instance.pk}.")


@receiver(post_save, sender=Booking)
def marketing_workflow_on_booking_completed(sender, instance, **kwargs):
    """HubSpot-style: optional automation when a class booking is completed."""
    try:
        from quickstart.services.marketing_workflow_triggers import (
            try_auto_enroll_booking_completed,
        )

        try_auto_enroll_booking_completed(instance)
    except Exception as e:
        logger.warning("marketing_workflow_on_booking_completed: %s", e, exc_info=True)


@receiver(post_save, sender=Booking)
def create_contact_on_first_booking(sender, instance, created, **kwargs):
    """When a booking is first created, ensure a Contact record exists for the user at the business."""
    if not instance.user or not created:
        return
    try:
        with transaction.atomic():
            user = instance.user
            business = instance.schedule_instance.schedule.option.classId.businessId
            contact, contact_created = Contact.objects.get_or_create(
                business=business,
                user=user,
                defaults={
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                    "email": user.email,
                    "phone_number": user.phone_number or "",
                    "source": "platform_booking",
                },
            )
            if contact_created:
                logger.info(
                    f"Automatically created Contact for user '{user.email}' at business '{business.businessName}' due to new booking."
                )
    except Exception as e:
        logger.error(
            f"Could not create Contact on booking for user {instance.user.userId}: {e}",
            exc_info=True,
        )


@receiver(post_save, sender=Notification)
def broadcast_notification_on_create(sender, instance, created, **kwargs):
    """Push new notification to user's WebSocket channel for real-time bell updates."""
    if created and instance.user_id:
        try:
            from quickstart.utils.notification_utils import broadcast_notification_to_user
            broadcast_notification_to_user(instance)
        except Exception as e:
            logger.warning("Failed to broadcast notification to WS: %s", e)


# ---------------------------------------------------------------------------
# Next.js (Cache Components) — on-demand revalidation via /api/revalidate
# ---------------------------------------------------------------------------


def _schedule_next_revalidate(tags):
    if not tags:
        return

    def _run():
        try:
            from quickstart.utils.next_revalidate import revalidate_next_cache_tags

            revalidate_next_cache_tags(list(tags))
        except Exception as e:
            logger.warning("Next.js cache revalidate skipped: %s", e)

    transaction.on_commit(_run)


@receiver(post_save, sender=ClassesMain)
def nextjs_revalidate_on_class_save(sender, instance, **kwargs):
    slug = getattr(instance, "slug", None) or ""
    if not slug:
        return
    _schedule_next_revalidate(
        [
            f"class-{slug}",
            "classes",
            "homepage-content",
            "collections",
            "classes-search",
        ]
    )
    _bust_homepage_sections_cache()


@receiver(post_save, sender=BusinessInfo)
def nextjs_revalidate_on_business_save(sender, instance, **kwargs):
    slug = getattr(instance, "slug", None) or ""
    if not slug:
        return
    _schedule_next_revalidate(
        [f"business-{slug}", "businesses", "public-businesses"]
    )


@receiver(post_save, sender=BlogPost)
@receiver(post_delete, sender=BlogPost)
def nextjs_revalidate_on_blog_post(sender, instance, **kwargs):
    slug = getattr(instance, "slug", None) or ""
    tags = ["blog-posts", "blog-categories", "blog-recent"]
    if slug:
        tags.append(f"blog-post-{slug}")
    _schedule_next_revalidate(tags)


@receiver(post_save, sender=ClassCollection)
@receiver(post_delete, sender=ClassCollection)
def nextjs_revalidate_on_collection(sender, instance, **kwargs):
    _schedule_next_revalidate(
        ["collections", "homepage-content", "classes-search"]
    )
    _bust_homepage_sections_cache()