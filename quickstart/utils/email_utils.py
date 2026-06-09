# quickstart/utils/email_utils.py

from datetime import datetime, timedelta
from decimal import Decimal
import logging
from icalendar import Calendar, Event, vRecur, vText
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
from django.utils import timezone
from typing import Any, Dict, List, Optional
import pytz

from CEBackend.celery import app as celery_app

from quickstart.utils.widget_booking_source import (
    business_has_growth_or_advanced_widget_plan,
    is_widget_booking_source,
)
from quickstart.utils.email_branding_constants import (
    EMAIL_TYPE_BOOKING_CANCELLATION_CONFIRMED,
    EMAIL_TYPE_BOOKING_CANCELLED_BY_HOST,
    EMAIL_TYPE_BOOKING_CONFIRMATION,
    EMAIL_TYPE_BOOKING_REMINDER,
    EMAIL_TYPE_BOOKING_RESCHEDULED,
    BRANDING_MODE_HTML,
)
from quickstart.utils.email_branding_html import (
    render_custom_html_template,
    wrap_email_html_fragment_if_needed,
)
from quickstart.utils.email_branding_placeholders import build_placeholder_map_from_context

from ..models import (
    ADDON_TYPE_EMAIL_MARKETING,
    ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
    Booking,
    BusinessStaff,
    CustomUser,
    GiftCard,
    Reviews,
    SupportTicket,
    BusinessInfo,
    VerificationRequest,
    Payout,
    ScheduleInstance,
    ClassesMain,
    Schedule,
    Contact,
    CourseEnrollment,
    Conversation,
    ConversationMessage,
    ConversationEmailLog,
    CustomerMembership,
)

logger = logging.getLogger(__name__)

_celery_task_found_and_logged = False
TASK_NAME = "quickstart.tasks.email_tasks.send_email_task"
TRANSACTIONAL_TASK_NAME = "quickstart.tasks.email_tasks.send_transactional_email_task"


def _cancellation_policy_display(policy_key, refund_pct=None, custom_hours=None):
    """Short human-readable cancellation policy for emails."""
    if not policy_key:
        return None
    key = (policy_key or "").lower()
    refund = "full refund" if (refund_pct or 0) == 100 else f"{refund_pct or 0}% refund"
    if key == "flexible":
        return f"Flexible: {refund} if you cancel at least 1 hour before."
    if key == "24h":
        return f"24-hour policy: {refund} if you cancel at least 24 hours before."
    if key == "48h":
        return f"48-hour policy: {refund} if you cancel at least 48 hours before."
    if key == "72h":
        return f"72-hour policy: {refund} if you cancel at least 72 hours before."
    if key == "custom" and custom_hours:
        return f"Cancellation: {refund} if you cancel at least {custom_hours} hours before."
    if key == "strict":
        return "Please note: this booking is final and not eligible for a refund after purchase."
    return "See cancellation policy in your booking details."


def _get_booking_related_data(booking: Booking) -> dict:
    data = {
        "class_title": "N/A",
        "class_id": None,
        "class_slug": None,
        "business_name": "N/A",
        "option_title": "N/A",
        "class_location": "N/A",
        "business_timezone": "UTC",
        "business_contact_email": settings.DEFAULT_FROM_EMAIL,
        "business_contact_phone": None,
        "schedule_summary": "N/A",
        "duration_minutes": None,
        "cancellation_policy_display": None,
        "equipment": "",
    }
    try:
        schedule_instance = getattr(booking, "schedule_instance", None)
        if not schedule_instance:
            raise AttributeError("schedule_instance missing")
        schedule = getattr(schedule_instance, "schedule", None)
        if not schedule:
            raise AttributeError("schedule missing")
        option = getattr(schedule, "option", None)
        if not option:
            raise AttributeError("option missing")
        class_main = getattr(option, "classId", None)
        if not class_main:
            raise AttributeError("classId missing from option")
        business = getattr(class_main, "businessId", None)
        if not business:
            raise AttributeError("businessId missing from class_main")

        data["class_title"] = getattr(class_main, "title", "N/A")
        data["class_id"] = getattr(class_main, "classId", None)
        data["class_slug"] = getattr(class_main, "slug", None)
        data["business_name"] = getattr(business, "businessName", "N/A")
        data["option_title"] = getattr(option, "title", "N/A")
        options_qs = getattr(class_main, "options", None)
        data["has_multiple_options"] = (
            options_qs.count() > 1 if options_qs is not None else False
        )
        data["class_location"] = getattr(class_main, "location", "N/A")
        data["business_timezone"] = getattr(business, "business_timezone", "UTC")
        data["business_contact_email"] = getattr(
            business, "studentContactEmail", settings.DEFAULT_FROM_EMAIL
        )
        data["business_contact_phone"] = getattr(
            business, "studentContactPhone", None
        ) or None
        data["duration_minutes"] = getattr(
            schedule_instance, "duration", None
        )
        equipment = getattr(option, "equipment", None)
        if isinstance(equipment, list):
            data["equipment"] = "\n".join(str(item) for item in equipment).strip()
        elif isinstance(equipment, str):
            data["equipment"] = equipment.strip()
        else:
            data["equipment"] = ""
        policy_key = getattr(booking, "cancellation_policy", None) or getattr(
            option, "cancellationPolicy", None
        )
        refund_pct = getattr(option, "cancellationRefundPercentage", None)
        custom_hours = getattr(option, "cancellationCustomHours", None)
        data["cancellation_policy_display"] = _cancellation_policy_display(
            policy_key, refund_pct, custom_hours
        )

        # Basic schedule summary for courses
        if schedule.day and schedule.time:
            data["schedule_summary"] = (
                f"{schedule.day}s at {schedule.time.strftime('%I:%M %p')}"
            )

    except AttributeError as e:
        logger.error(
            f"Could not access related data for booking {booking.id} when preparing email context: {e}",
            exc_info=True,
        )
    return data


def _generate_ics_content(
    booking: Booking, related_data: Dict[str, Any], user: CustomUser
) -> Optional[str]:
    if not booking.schedule_instance:
        logger.warning(
            f"Cannot generate ICS for booking {booking.id}: schedule_instance is missing."
        )
        return None

    try:
        cal = Calendar()
        cal.add("prodid", f"-//ClassEasily Booking//classeasily.com//EN")
        cal.add("version", "2.0")
        cal.add("method", "REQUEST")

        event = Event()
        class_title = related_data.get("class_title", "Class Booking")
        option_title = related_data.get("option_title", "")
        event_summary = f"{class_title}{f' - {option_title}' if option_title and option_title != class_title else ''}"
        event.add("summary", vText(event_summary))

        business_timezone_str = related_data.get("business_timezone", "UTC")
        try:
            business_tz = pytz.timezone(business_timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.error(
                f"Unknown business timezone '{business_timezone_str}' for booking {booking.id}. Defaulting to UTC for ICS."
            )
            business_tz = pytz.utc

        schedule_inst = booking.schedule_instance
        naive_start_dt = datetime.combine(schedule_inst.date, schedule_inst.time)
        aware_start_business = business_tz.localize(naive_start_dt)

        aware_start_utc = aware_start_business.astimezone(pytz.utc)
        aware_end_utc = aware_start_utc + timedelta(minutes=schedule_inst.duration)

        event.add("dtstart", aware_start_utc)
        event.add("dtend", aware_end_utc)
        event.add("dtstamp", datetime.utcnow().replace(tzinfo=pytz.utc))

        uid_domain = settings.SITE_DOMAIN or "classeasily.com"
        event.add(
            "uid",
            f'{booking.user_facing_reference or booking.id}-{schedule_inst.date.strftime("%Y%m%d")}@{uid_domain}',
        )

        event.add("location", vText(related_data.get("class_location", "N/A")))

        description_parts = [
            f"Your booking for: {event_summary}",
            f"Business: {related_data.get('business_name', 'N/A')}",
            f"Reference: {booking.user_facing_reference or f'ID #{booking.id}'}",
            f"Participants: {booking.participants}",
        ]
        if booking.notes:
            description_parts.append(f"Your Notes: {booking.notes}")
        event.add("description", vText("\n".join(description_parts)))

        organizer_email = related_data.get(
            "business_contact_email", settings.DEFAULT_FROM_EMAIL
        )
        event.add(
            "organizer",
            f"MAILTO:{organizer_email}",
            parameters={
                "CN": vText(related_data.get("business_name", "ClassEasily Business"))
            },
        )

        if user and hasattr(user, "email") and user.email:
            attendee_name = (
                user.get_full_name()
                if hasattr(user, "get_full_name")
                else f"{user.first_name} {user.last_name}"
            )
            attendee_cn = vText(attendee_name or user.email)
            event.add(
                "attendee",
                f"MAILTO:{user.email}",
                parameters={
                    "CN": attendee_cn,
                    "ROLE": "REQ-PARTICIPANT",
                    "PARTSTAT": "NEEDS-ACTION",
                    "RSVP": "TRUE",
                },
            )

        event.add("status", "CONFIRMED")
        event.add("transp", "OPAQUE")

        cal.add_component(event)
        return cal.to_ical().decode("utf-8")

    except Exception as e:
        logger.error(
            f"Failed to generate ICS content for booking {booking.id}: {e}",
            exc_info=True,
        )
        return None


def send_templated_email(
    recipient_list: List[str],
    template_name: str,
    context: Dict[str, Any],
    subject: Optional[str] = None,
    from_email: Optional[str] = None,
    attachments: Optional[List[Dict]] = None,
):
    """
    Renders an email template, queues it for sending via a Celery task
    """
    from quickstart.tasks.email_tasks import send_transactional_email_task

    logger.info(
        f"Attempting to queue email via send_templated_email. Template: '{template_name}', Recipients: {recipient_list}"
    )

    if not recipient_list or not isinstance(recipient_list, list):
        logger.error(
            f"send_templated_email was called with an invalid or empty recipient_list for template '{template_name}'. Aborting."
        )
        return

    try:
        # Ensure settings are always available in the template context
        template_context = context.copy()
        template_context["settings"] = settings

        # Render the HTML content from the specified template
        html_content = render_to_string(template_name, template_context)

        # If no subject is provided, generate a default one from the template name
        if not subject:
            template_base = (
                template_name.split("/")[-1]
                .replace(".html", "")
                .replace("_", " ")
                .title()
            )
            subject = f"{template_base} - ClassEasily"

        # Truncate subject if it's excessively long to prevent email client issues
        max_subject_length = 150
        if len(subject) > max_subject_length:
            subject = subject[:max_subject_length] + "..."
            logger.warning(
                f"Subject for template '{template_name}' was too long and has been truncated."
            )

        # Prepare arguments for the Celery task
        task_kwargs = {
            "subject": subject,
            "html": html_content,
            "to": recipient_list,
            "from_email": from_email or settings.DEFAULT_FROM_EMAIL,
        }

        if attachments:
            task_kwargs["attachments"] = attachments

        # Asynchronously queue the email sending task
        task_result = send_transactional_email_task.delay(**task_kwargs)
        logger.info(
            f"✅ Email from template '{template_name}' successfully queued for {recipient_list}. Task ID: {task_result.id}"
        )
        return task_result

    except Exception as e:
        # This block catches errors during template rendering (e.g., a missing variable)
        logger.error(
            f"CRITICAL: Failed to render email template '{template_name}'. Error: {e}",
            exc_info=True,
        )

        # If template rendering fails, send a generic fallback email to the user
        fallback_subject = "Important Notification from ClassEasily"
        fallback_html = "<p>We tried to send you an email, but a server error prevented it from being created correctly. Our team has been notified.</p><p>If you were expecting a booking confirmation or password reset, please contact our support team for assistance.</p><p>We apologize for the inconvenience.</p>"

        fallback_kwargs = {
            "subject": fallback_subject,
            "html": fallback_html,
            "to": recipient_list,
            "from_email": from_email or settings.DEFAULT_FROM_EMAIL,
        }

        task_result = send_transactional_email_task.delay(**fallback_kwargs)
        logger.warning(
            f"⚠️ A fallback email was sent to {recipient_list} due to a template rendering error. Task ID: {task_result.id}"
        )
        return task_result


def send_transactional_html_email(
    recipient_list: List[str],
    html: str,
    subject: Optional[str] = None,
    from_email: Optional[str] = None,
    attachments: Optional[List[Dict]] = None,
):
    """
    Queue a pre-built HTML transactional email (bypasses Django template rendering).
    """
    from quickstart.tasks.email_tasks import send_transactional_email_task

    if not recipient_list or not isinstance(recipient_list, list):
        logger.error("send_transactional_html_email: invalid recipient_list")
        return
    if not html or not str(html).strip():
        logger.error("send_transactional_html_email: empty html")
        return
    subj = subject or "Notification from ClassEasily"
    max_subject_length = 150
    if len(subj) > max_subject_length:
        subj = subj[:max_subject_length] + "..."
    task_kwargs = {
        "subject": subj,
        "html": html,
        "to": recipient_list,
        "from_email": from_email or settings.DEFAULT_FROM_EMAIL,
    }
    if attachments:
        task_kwargs["attachments"] = attachments
    return send_transactional_email_task.delay(**task_kwargs)


def _attach_booking_email_branding(context: Dict[str, Any], booking: Booking, booking_source=None):
    """
    Attach email_branding, header_logo_* to context when widget or marketplace branding applies.
    """
    if not booking or not getattr(booking, "schedule_instance", None):
        return
    try:
        business = (
            booking.schedule_instance.schedule.option.classId.businessId
        )
        if is_widget_booking_source(booking_source) and business_has_growth_or_advanced_widget_plan(
            business
        ):
            branding = getattr(business, "widget_email_branding", None) or {}
            if branding:
                context["email_branding"] = branding
                logo_url = (branding.get("logo_url") or "").strip()
                if logo_url:
                    context["header_logo_url"] = logo_url
                    try:
                        _w = branding.get("logo_max_width")
                        _h = branding.get("logo_max_height")
                        context["header_logo_max_width"] = int(_w) if _w not in (None, "") else 160
                        context["header_logo_max_height"] = int(_h) if _h not in (None, "") else 60
                    except (TypeError, ValueError):
                        context["header_logo_max_width"] = 160
                        context["header_logo_max_height"] = 60
        elif getattr(business, "marketplace_email_branding_enabled", False):
            branding = getattr(business, "marketplace_email_branding", None) or {}
            if branding:
                context["email_branding"] = branding
                logo_url = (branding.get("logo_url") or "").strip()
                if logo_url:
                    context["header_logo_url"] = logo_url
                    try:
                        _w = branding.get("logo_max_width")
                        _h = branding.get("logo_max_height")
                        context["header_logo_max_width"] = int(_w) if _w not in (None, "") else 160
                        context["header_logo_max_height"] = int(_h) if _h not in (None, "") else 60
                    except (TypeError, ValueError):
                        context["header_logo_max_width"] = 160
                        context["header_logo_max_height"] = 60
    except Exception as e:
        logger.warning(
            "Could not attach email branding for booking %s: %s",
            getattr(booking, "id", None),
            e,
        )


def _try_send_custom_branding_html_email(
    recipient_list: List[str],
    email_type_key: str,
    context: Dict[str, Any],
    subject: str,
    attachments: Optional[List[Dict]] = None,
) -> bool:
    """
    If branding mode is html and custom_html[email_type_key] is set, send that and return True.
    """
    branding = context.get("email_branding") or {}
    if (branding.get("mode") or "builder") != BRANDING_MODE_HTML:
        return False
    custom = branding.get("custom_html") or {}
    raw = custom.get(email_type_key)
    if raw is None or not str(raw).strip():
        return False
    ph_map = build_placeholder_map_from_context(email_type_key, context)
    body = render_custom_html_template(str(raw), ph_map)
    wrapped = wrap_email_html_fragment_if_needed(body, title=subject or "Email")
    send_transactional_html_email(
        recipient_list=recipient_list,
        html=wrapped,
        subject=subject,
        attachments=attachments,
    )
    return True


def send_booking_confirmation_email(user, booking: Booking, booking_source=None, override_recipient_list=None):
    """
    Sends a booking confirmation email to either a registered user (CustomUser)
    or a guest (Contact).
    booking_source: optional "widget" | "member_widget" | "marketplace". Widget-sourced
    bookings use widget_email_branding when the business has Growth/Advanced and branding is set.
    override_recipient_list: optional list of email addresses to send to instead of recipient.email (e.g. for resend).
    """
    logger.info(
        f"--- send_booking_confirmation_email initiated for Booking ID: {booking.id} ---"
    )
    logger.info(f"Recipient object type: {type(user)}")

    recipient = user
    if (
        not recipient
        or not hasattr(recipient, "email")
        or not recipient.email
        or not booking
    ):
        logger.error(
            f"send_booking_confirmation_email HALTED: Invalid recipient or booking. Recipient valid: {bool(recipient)}, Booking valid: {bool(booking)}"
        )
        return

    logger.info(f"Recipient email address: {recipient.email}")

    related_data = _get_booking_related_data(booking)
    class_identifier = related_data.get("class_slug") or related_data.get("class_id")
    if not class_identifier:
        logger.error(
            f"Could not access essential related data for booking {booking.id} when sending confirmation."
        )

    class_details_url = (
        f"{settings.FRONTEND_BASE_URL}/classes/{class_identifier}"
        if class_identifier
        else "#"
    )
    # Updated: Deep link to the specific booking in "upcoming" tab
    manage_bookings_url = (
        f"{settings.FRONTEND_BASE_URL}/my-classes?tab=upcoming&highlight={booking.id}"
    )

    payment = (
        booking.payments.filter(status="succeeded").order_by("-created_at").first()
    )

    is_guest_flag = not isinstance(recipient, CustomUser)
    logger.info(f"Determined recipient is_guest status: {is_guest_flag}")

    formatted_time_range = "N/A"
    formatted_timezone_display = related_data.get("business_timezone", "UTC")

    if booking.schedule_instance:
        try:
            # 1. Calculate End Time
            start_time = booking.schedule_instance.time
            duration_minutes = booking.schedule_instance.duration

            # Combine with today's date just to do the math easily
            dummy_date = datetime.now().date()
            start_dt = datetime.combine(dummy_date, start_time)
            end_dt = start_dt + timedelta(minutes=duration_minutes)

            # Format: "7:00 PM - 8:00 PM"
            time_str_start = start_dt.strftime("%-I:%M %p")
            time_str_end = end_dt.strftime("%-I:%M %p")
            formatted_time_range = f"{time_str_start} - {time_str_end}"

            # 2. Format Timezone (e.g., "America/New_York" -> "America/New York")
            if formatted_timezone_display:
                formatted_timezone_display = formatted_timezone_display.replace(
                    "_", " "
                )
        except Exception as e:
            logger.error(f"Error formatting dates for email: {e}")
            formatted_time_range = str(booking.schedule_instance.time)

    formatted_duration_minutes = None
    if booking.schedule_instance and getattr(booking.schedule_instance, "duration", None):
        d = booking.schedule_instance.duration
        formatted_duration_minutes = f"{d} min" if d else None

    context = {
        "user": recipient,
        "booking": booking,
        "class_details_url": class_details_url,
        "manage_bookings_url": manage_bookings_url,
        "recipient_email": recipient.email,
        "related_data": related_data,
        "payment": payment,
        "is_guest": is_guest_flag,
        "formatted_time_range": formatted_time_range,
        "formatted_timezone_display": formatted_timezone_display,
        "formatted_duration_minutes": formatted_duration_minutes,
    }
    if booking.schedule_instance:
        _attach_booking_email_branding(context, booking, booking_source)

    if context["is_guest"] and booking.cancellation_token:
        guest_cancellation_url = (
            f"{settings.FRONTEND_BASE_URL}/guest/cancel/{booking.cancellation_token}"
        )
        context["guest_cancellation_url"] = guest_cancellation_url
        logger.info(
            f"Adding guest cancellation URL to email context for booking {booking.id}"
        )
    elif context["is_guest"] and not booking.cancellation_token:
        logger.warning(
            f"Guest booking {booking.id} is missing a cancellation token for the email."
        )

    # Determine Template based on enrollment type
    if booking.enrollment_type == "Full Course":
        template_name = "emails/course_confirmation_user.html"
        subject_prefix = "Course Enrollment Confirmed:"

        # --- NEW LOGIC: Fetch full course session list ---
        if booking.booking_group_id:
            try:
                enrollment = CourseEnrollment.objects.get(
                    booking_group_id=booking.booking_group_id
                )
                context["enrollment"] = enrollment

                # Fetch all sessions to list specific dates
                course_sessions = (
                    Booking.objects.filter(booking_group_id=booking.booking_group_id)
                    .select_related("schedule_instance")
                    .order_by("course_session_number")
                )

                context["course_sessions"] = course_sessions
                logger.info(
                    f"Attached {course_sessions.count()} session dates to email context."
                )
            except CourseEnrollment.DoesNotExist:
                logger.warning(
                    f"CourseEnrollment missing for booking_group_id {booking.booking_group_id}"
                )
                context["enrollment"] = None
                context["course_sessions"] = []
    else:
        template_name = "emails/booking_confirmation_user.html"
        subject_prefix = "Booking Confirmed:"

    ics_content_str = _generate_ics_content(booking, related_data, recipient)
    email_attachments = None
    if ics_content_str:
        filename_class_part = (
            (related_data.get("class_title", "class")[:20])
            .replace(" ", "_")
            .replace("/", "_")
        )
        ics_filename = f"{filename_class_part}_booking_{booking.schedule_instance.date.strftime('%Y%m%d')}.ics"
        email_attachments = [
            {
                "filename": ics_filename,
                "content": ics_content_str,
                "mimetype": "text/calendar; charset=utf-8; method=REQUEST",
            }
        ]
        logger.info(
            f"Generated ICS attachment: {ics_filename} for booking {booking.id}"
        )
    else:
        logger.warning(f"Could not generate ICS attachment for booking {booking.id}")

    to_list = override_recipient_list if override_recipient_list is not None else [recipient.email]
    subject_line = f"{subject_prefix} {related_data.get('class_title', '[Class Title]')}"
    custom_key = (
        EMAIL_TYPE_BOOKING_CONFIRMATION
        if booking.enrollment_type != "Full Course"
        else None
    )
    if custom_key and _try_send_custom_branding_html_email(
        to_list,
        custom_key,
        context,
        subject_line,
        attachments=email_attachments,
    ):
        logger.info(
            f"--- send_booking_confirmation_email (custom HTML) finished for Booking ID: {booking.id} ---"
        )
        return

    logger.info(
        f"Proceeding to call send_templated_email for booking {booking.id} using template '{template_name}' to {to_list}"
    )

    send_templated_email(
        recipient_list=to_list,
        template_name=template_name,
        context=context,
        subject=subject_line,
        attachments=email_attachments,
    )

    logger.info(
        f"--- send_booking_confirmation_email finished for Booking ID: {booking.id} ---"
    )


def send_business_staff_invitation_email(invitation: BusinessStaff):
    """Sends an invitation email to a new potential staff member."""
    if not invitation or not invitation.invited_email:
        logger.warning(
            "Attempted to send staff invitation with invalid invitation object."
        )
        return

    logger.info(f"Preparing staff invitation email for {invitation.invited_email}")

    accept_url = f"{settings.FRONTEND_BASE_URL}/join-business?token={invitation.invitation_token}"

    context = {
        "inviter_name": invitation.invited_by.get_full_name(),
        "business_name": invitation.business.businessName,
        "role_name": invitation.role.name,
        "accept_url": accept_url,
        "recipient_email": invitation.invited_email,
    }

    send_templated_email(
        recipient_list=[invitation.invited_email],
        template_name="emails/business_staff_invitation.html",
        context=context,
        subject=f"You're invited to join {invitation.business.businessName} on ClassEasily",
    )


def send_gift_card_email(gift_card):
    """
    Sends the gift card code/details to the recipient.
    Used for both instant delivery and scheduled delivery tasks.
    """
    if not gift_card or not gift_card.recipient_email:
        logger.warning(
            "Attempted to send gift card email without valid card or recipient."
        )
        return

    logger.info(
        f"Preparing gift card email for {gift_card.code} to {gift_card.recipient_email}"
    )

    # Determine if this is a self-purchase (reliable if we have the flag, else infer from names)
    is_self = getattr(gift_card, "send_to_self", False)
    if not is_self and gift_card.sender_name and gift_card.recipient_name:
        if (
            gift_card.sender_name.lower().strip()
            in gift_card.recipient_name.lower().strip()
        ):
            is_self = True

    # Only use design_url in email if it's an absolute URL (many clients block relative URLs)
    design_image_url = None
    if gift_card.design_url and str(gift_card.design_url).strip().lower().startswith("http"):
        design_image_url = gift_card.design_url

    context = {
        "gift_card": gift_card,
        "is_self": is_self,
        "recipient_email": gift_card.recipient_email,
        "design_image_url": design_image_url,
    }

    send_templated_email(
        recipient_list=[gift_card.recipient_email],
        template_name="emails/gift_card_delivery.html",
        context=context,
        subject=f"You've received a ${gift_card.initial_amount} Gift Card!",
    )

    # Mark as sent
    gift_card.email_sent = True
    gift_card.save(update_fields=["email_sent"])


def send_account_security_email(user, change_type, new_email=None, subject=None):
    """
    Sends a security notification after password or email change.
    """
    if not user or not user.email:
        logger.warning("Attempted to send security email to invalid user.")
        return

    recipient = user.email

    logger.info(
        f"Preparing security email (type: {change_type}) for user {user.userId} to {recipient}"
    )

    context = {
        "user": user,
        "change_type": change_type,
        "change_time": timezone.now(),
        "recipient_email": recipient,
        "new_email_address": new_email,
    }
    send_templated_email(
        recipient_list=[recipient],
        template_name="emails/account_security_change.html",
        context=context,
        subject=subject,
    )


def send_refund_failed_guest_email(booking: Booking, reason: str):
    """
    Notify the guest that their cancellation refund could not be completed automatically
    and that support will follow up.
    """
    if not booking:
        logger.warning("Attempted to send refund failed email with no booking.")
        return
    guest_email = None
    guest_user = None
    if getattr(booking, "user", None) and getattr(booking.user, "email", None):
        guest_email = booking.user.email
        guest_user = booking.user
    elif getattr(booking, "contact", None) and getattr(booking.contact, "email", None):
        guest_email = booking.contact.email
    if not guest_email:
        logger.warning("Booking %s has no guest email for refund failed notification.", booking.id)
        return
    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    context = {
        "user": guest_user,
        "recipient_email": guest_email,
        "booking_id": booking.id,
        "reason": reason or "Unknown error",
        "my_classes_url": f"{base}/my-classes" if base else "/my-classes",
        "my_tickets_url": f"{base}/my-tickets" if base else "/my-tickets",
    }
    send_templated_email(
        recipient_list=[guest_email],
        template_name="emails/refund_failed_guest.html",
        context=context,
        subject="Your refund could not be completed – we'll follow up",
    )


def send_booking_cancellation_user_email(
    user, booking: Booking, refund_details: str, booking_source=None
):
    """
    Sends confirmation to a user after they cancelled their booking.
    booking_source: optional widget / member_widget / marketplace for branding resolution.
    """
    if not user or not user.email or not booking:
        logger.warning(
            "Attempted to send user booking cancellation email with invalid user or booking."
        )
        return

    related_data = _get_booking_related_data(booking)
    if not related_data.get("class_title"):
        logger.error(
            f"Could not access class_title for booking {booking.id} when sending user cancellation email."
        )

    logger.info(
        f"Preparing user booking cancellation email for booking {booking.id} to user {user.email}"
    )

    explore_url = f"{settings.FRONTEND_BASE_URL}/explore"

    context = {
        "user": user,
        "booking": booking,
        "refund_details": refund_details,
        "explore_url": explore_url,
        "recipient_email": user.email,
        "related_data": related_data,
        "upcoming_sessions": [],
    }
    if booking.schedule_instance:
        _attach_booking_email_branding(context, booking, booking_source)

    # Choose template and subject based on type
    if booking.enrollment_type == "Full Course":
        template_name = "emails/course_cancellation_user.html"

        # Determine if it's a full drop or single session
        # If the CourseEnrollment is "dropped" or "cancelled", it's the whole thing
        # If just this booking is "cancelled", it's a single session

        is_single_session_drop = False
        try:
            # Check if user has other active bookings in this group
            if booking.booking_group_id:
                active_siblings = Booking.objects.filter(
                    booking_group_id=booking.booking_group_id, status="confirmed"
                ).count()
                if active_siblings > 0:
                    is_single_session_drop = True
                    subject_prefix = (
                        f"Session {booking.course_session_number} Cancelled:"
                    )

                    # Fetch upcoming sessions
                    next_sessions = (
                        Booking.objects.filter(
                            booking_group_id=booking.booking_group_id,
                            status="confirmed",
                            schedule_instance__date__gt=booking.schedule_instance.date,
                        )
                        .select_related("schedule_instance")
                        .order_by("course_session_number")[:3]
                    )
                    context["upcoming_sessions"] = next_sessions
                else:
                    subject_prefix = "Course Drop Confirmed:"
        except Exception as e:
            logger.error(f"Error determining drop type for email: {e}")
            subject_prefix = "Cancellation Confirmed:"

        context["is_single_session_drop"] = is_single_session_drop
        logger.info(f"Using Course Cancellation template for booking {booking.id}")
    else:
        template_name = "emails/booking_cancellation_user.html"
        subject_prefix = "Booking Cancelled:"
        logger.info(
            f"Using Single Session Cancellation template for booking {booking.id}"
        )

    subject_line = f"{subject_prefix} {related_data.get('class_title', '[Class Title]')}"
    custom_key = (
        EMAIL_TYPE_BOOKING_CANCELLATION_CONFIRMED
        if booking.enrollment_type != "Full Course"
        else None
    )
    if custom_key and _try_send_custom_branding_html_email(
        [user.email],
        custom_key,
        context,
        subject_line,
        attachments=None,
    ):
        logger.info(
            f"User booking cancellation custom HTML email queued for booking {booking.id}"
        )
        return

    send_templated_email(
        recipient_list=[user.email],
        template_name=template_name,
        context=context,
        subject=subject_line,
    )
    logger.info(
        f"User booking cancellation email prepared/queued for booking {booking.id}"
    )


def send_booking_cancelled_by_other_email(
    user,
    booking: Booking,
    cancelled_by: str,
    reason: str,
    contact_info: str,
    booking_source=None,
):
    """
    Sends notification to a user when their booking is cancelled by the business or an admin.
    booking_source: optional widget / member_widget / marketplace for branding resolution.
    """
    if not user or not user.email or not booking:
        logger.warning(
            "Attempted to send 'cancelled by other' email with invalid user or booking."
        )
        return

    related_data = _get_booking_related_data(booking)
    if not related_data.get("class_title"):
        logger.error(
            f"Could not access related data for booking {booking.id} when sending 'cancelled by other' email."
        )

    logger.info(
        f"Preparing 'cancelled by other' email for booking {booking.id} to user {user.email}"
    )

    explore_url = f"{settings.FRONTEND_BASE_URL}/explore"

    context = {
        "user": user,
        "booking": booking,
        "cancelled_by": cancelled_by,
        "reason": reason or "No specific reason provided.",
        "contact_info": contact_info,
        "explore_url": explore_url,
        "recipient_email": user.email,
        "related_data": related_data,
        "upcoming_sessions": [],
    }

    # --- NEW LOGIC: Fetch upcoming sessions if it's a course session ---
    if booking.enrollment_type == "Full Course" and booking.booking_group_id:
        try:
            # Check for active future sessions in the same course
            upcoming_sessions = (
                Booking.objects.filter(
                    booking_group_id=booking.booking_group_id,
                    status="confirmed",
                    schedule_instance__date__gt=booking.schedule_instance.date,
                )
                .select_related("schedule_instance")
                .order_by("course_session_number")[:3]
            )

            context["upcoming_sessions"] = upcoming_sessions
            if upcoming_sessions.exists():
                logger.info(
                    f"Found {upcoming_sessions.count()} upcoming sessions to list in cancellation email."
                )
        except Exception as e:
            logger.warning(
                f"Failed to fetch upcoming sessions for cancellation email: {e}"
            )

    if booking.schedule_instance:
        _attach_booking_email_branding(context, booking, booking_source)

    subject_line = f"Important: Your Class {related_data.get('class_title', '[Class Title]')} Was Cancelled"
    if _try_send_custom_branding_html_email(
        [user.email],
        EMAIL_TYPE_BOOKING_CANCELLED_BY_HOST,
        context,
        subject_line,
        attachments=None,
    ):
        logger.info(
            f"'Cancelled by other' custom HTML email prepared/queued for booking {booking.id}"
        )
        return

    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/booking_cancelled_by_other.html",
        context=context,
        subject=subject_line,
    )
    logger.info(f"'Cancelled by other' email prepared/queued for booking {booking.id}")


def send_booking_reminder_email(user, booking: Booking, booking_source=None):
    """
    Sends a reminder email to a user about an upcoming class.
    booking_source: optional "widget" | "member_widget" | "marketplace". Widget-sourced
    reminders use widget_email_branding when the business has Growth/Advanced and branding is set.
    """
    if not user or not user.email or not booking:
        logger.warning(
            "Attempted to send booking reminder with invalid user or booking."
        )
        return

    related_data = _get_booking_related_data(booking)
    class_identifier = related_data.get("class_slug") or related_data.get("class_id")
    if not class_identifier:
        logger.error(
            f"Could not access class identifier for booking {booking.id} when sending reminder email."
        )

    logger.info(
        f"Preparing booking reminder email for booking {booking.id} to user {user.email}"
    )

    class_details_url = (
        f"{settings.FRONTEND_BASE_URL}/classes/{class_identifier}"
        if class_identifier
        else "#"
    )

    # Updated: Highlighting the specific booking
    manage_bookings_url = (
        f"{settings.FRONTEND_BASE_URL}/my-classes?tab=upcoming&highlight={booking.id}"
    )

    # Calculate end time
    calculated_end_time = None
    if booking.schedule_instance:
        try:
            dummy_date = datetime.now().date()
            start_dt = datetime.combine(dummy_date, booking.schedule_instance.time)
            end_dt = start_dt + timedelta(minutes=booking.schedule_instance.duration)
            calculated_end_time = end_dt.time()
        except Exception as e:
            logger.error(f"Error calculating end time for booking {booking.id}: {e}")

    # Format Timezone (Replace underscore with space)
    formatted_timezone = related_data.get("business_timezone", "UTC")
    if formatted_timezone:
        formatted_timezone = formatted_timezone.replace("_", " ")

    context = {
        "user": user,
        "booking": booking,
        "class_details_url": class_details_url,
        "manage_bookings_url": manage_bookings_url,  # Added this to context
        "recipient_email": user.email,
        "related_data": related_data,
        "calculated_end_time": calculated_end_time,
        "formatted_timezone": formatted_timezone,
    }
    if booking.schedule_instance:
        _attach_booking_email_branding(context, booking, booking_source)

    # Pass course session context if available
    if booking.enrollment_type == "Full Course" and booking.course_session_number:
        context["is_course_session"] = True
        subject_prefix = f"Reminder: Session {booking.course_session_number} of"
        logger.info(
            f"Formatting reminder for Course Session #{booking.course_session_number}"
        )
    else:
        context["is_course_session"] = False
        subject_prefix = "Reminder: Your Class"
        logger.info("Formatting reminder for Single Session")

    subject_line = f"{subject_prefix} {related_data.get('class_title', '[Class Title]')} is Soon!"
    if _try_send_custom_branding_html_email(
        [user.email],
        EMAIL_TYPE_BOOKING_REMINDER,
        context,
        subject_line,
        attachments=None,
    ):
        logger.info(
            f"Booking reminder custom HTML email prepared/queued for booking {booking.id}"
        )
        return

    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/booking_reminder_user.html",
        context=context,
        subject=subject_line,
    )
    logger.info(f"Booking reminder email prepared/queued for booking {booking.id}")


def send_admin_new_verification_request_email(
    admin_recipient_list: List[str], verification_request: VerificationRequest
):
    """Notifies designated admins about a new verification request."""
    if not admin_recipient_list:
        logger.warning(
            "No admin recipients provided for new verification request notification."
        )
        return
    if not verification_request:
        logger.warning(
            "Attempted to send new verification request email with invalid request object."
        )
        return

    try:
        user = verification_request.user
        business = verification_request.business
        business_name = business.businessName if business else "N/A"
        user_email = user.email if user else "N/A"
    except AttributeError:
        logger.error(
            f"Could not access related data for verification request {verification_request.id} when sending admin notification."
        )
        return

    logger.info(
        f"Preparing new verification request notification email for request {verification_request.id} to admins: {admin_recipient_list}"
    )

    verification_url = f"{settings.FRONTEND_BASE_URL}/admin/business-verification"

    context = {
        "verification_request": verification_request,
        "user": user,
        "business": business,
        "verification_url": verification_url,
        "recipient_email": ", ".join(admin_recipient_list),
    }
    send_templated_email(
        recipient_list=admin_recipient_list,
        template_name="emails/admin_new_verification_request.html",
        context=context,
        subject=f"New Verification Request Submitted: {business_name} ({user_email})",
    )
    logger.info(
        f"New verification request email prepared/queued for request {verification_request.id}"
    )


def get_super_admin_emails() -> List[str]:
    """Return list of email addresses for all active users with the Super Admin role."""
    return list(
        CustomUser.objects.filter(
            role__name="Super Admin",
            is_active=True,
        )
        .exclude(email__isnull=True)
        .exclude(email="")
        .values_list("email", flat=True)
        .distinct()
    )


def _get_booker_display(booking: Booking) -> dict:
    """Return display info for the user/guest who made the booking."""
    out = {"name": "Unknown", "email": "N/A", "is_guest": False}
    if booking.user:
        out["name"] = booking.user.get_full_name() or booking.user.email or "N/A"
        out["email"] = getattr(booking.user, "email", "N/A") or "N/A"
    elif booking.contact:
        name_parts = [
            n for n in [getattr(booking.contact, "first_name", ""), getattr(booking.contact, "last_name", "")]
            if n
        ]
        out["name"] = " ".join(name_parts) if name_parts else getattr(booking.contact, "email", "N/A")
        out["email"] = getattr(booking.contact, "email", "N/A") or "N/A"
        out["is_guest"] = True
    return out


def _is_placeholder_booker_email(email: str) -> bool:
    """True if the email is a known placeholder (e.g. guest checkout before real email is set)."""
    if not email or email == "N/A":
        return True
    return "pending@example" in (email or "").lower()


def _is_placeholder_phone(phone: str) -> bool:
    """True if the phone is a known placeholder (e.g. 555-555-5555)."""
    if not phone or not str(phone).strip():
        return True
    normalized = "".join(c for c in str(phone) if c.isdigit())
    return normalized == "5555555555" or "555-555-5555" in str(phone).replace(" ", "")


def is_placeholder_guest_contact(contact) -> bool:
    """True if the contact has placeholder email or phone (used to block completing bookings with mock data)."""
    if not contact:
        return False
    email = getattr(contact, "email", None) or ""
    phone = getattr(contact, "phone_number", None) or ""
    return _is_placeholder_booker_email(email) or _is_placeholder_phone(phone)


def send_super_admin_booking_created_email(booking: Booking):
    """Email all Super Admins when a new booking is made. Includes booking and booker info."""
    recipient_list = get_super_admin_emails()
    if not recipient_list:
        logger.debug("No Super Admin recipients for new booking notification; skipping email.")
        return
    if not booking:
        logger.warning("send_super_admin_booking_created_email called with no booking.")
        return
    related_data = _get_booking_related_data(booking)
    booker = _get_booker_display(booking)
    # Skip placeholder contacts so we don't send a duplicate/broken email for pending guest bookings.
    if _is_placeholder_booker_email(booker.get("email") or ""):
        logger.debug(
            "Skipping Super Admin new-booking email for booking %s (placeholder booker email).",
            booking.id,
        )
        return
    admin_url = f"{settings.FRONTEND_BASE_URL}/admin"
    context = {
        "booking": booking,
        "related_data": related_data,
        "booker": booker,
        "admin_url": admin_url,
        "recipient_email": ", ".join(recipient_list),
    }
    send_templated_email(
        recipient_list=recipient_list,
        template_name="emails/super_admin_booking_created.html",
        context=context,
        subject=f"[ClassEasily] New Booking: {related_data.get('class_title', 'N/A')} by {booker['name']}",
    )
    logger.info(
        f"Super Admin new-booking email prepared/queued for booking {booking.id} to {len(recipient_list)} Super Admin(s)."
    )


def send_super_admin_booking_webhook_failed_email(
    payment_intent_id: str,
    error_message: str,
    *,
    webhook_id: str = "",
    metadata: Optional[dict] = None,
    amount_received_cents: Optional[int] = None,
):
    """
    Email Super Admins when a succeeded Stripe payment could not be fulfilled via webhook.
    Payment is not auto-refunded; ops must create the booking and/or contact the guest.
    """
    recipient_list = get_super_admin_emails()
    if not recipient_list:
        logger.debug(
            "No Super Admin recipients for booking webhook failure; skipping email."
        )
        return
    metadata = metadata or {}
    guest_name = (metadata.get("guest_full_name") or "").strip() or "—"
    guest_email = (metadata.get("guest_email") or "").strip() or "—"
    guest_phone = (metadata.get("guest_phone") or "").strip() or "—"
    schedule_instance_id = metadata.get("schedule_instance_id") or "—"
    amount_display = "—"
    if amount_received_cents is not None:
        amount_display = f"${Decimal(amount_received_cents) / 100:.2f} CAD"
    admin_url = f"{settings.FRONTEND_BASE_URL}/admin"
    stripe_url = (
        f"https://dashboard.stripe.com/payments/{payment_intent_id}"
        if payment_intent_id
        else admin_url
    )
    context = {
        "payment_intent_id": payment_intent_id or "—",
        "error_message": error_message or "Unknown error",
        "webhook_id": webhook_id or "—",
        "guest_name": guest_name,
        "guest_email": guest_email,
        "guest_phone": guest_phone,
        "schedule_instance_id": schedule_instance_id,
        "amount_display": amount_display,
        "admin_url": admin_url,
        "stripe_url": stripe_url,
        "recipient_email": ", ".join(recipient_list),
    }
    send_templated_email(
        recipient_list=recipient_list,
        template_name="emails/super_admin_booking_webhook_failed.html",
        context=context,
        subject=(
            f"[ClassEasily] Paid booking webhook failed — manual action required "
            f"({payment_intent_id})"
        ),
    )
    logger.info(
        "Super Admin booking-webhook-failure email queued for PI=%s to %s recipient(s).",
        payment_intent_id,
        len(recipient_list),
    )


def send_super_admin_payout_failed_email(
    business_id: int,
    error_message: str,
    business: Optional[BusinessInfo] = None,
    *,
    stripe_error: bool = False,
):
    """
    Email all Super Admins when daily payout processing fails for a business.
    The business is not notified (ops handles Stripe / reconciliation).
    """
    recipient_list = get_super_admin_emails()
    if not recipient_list:
        logger.debug(
            "No Super Admin recipients for payout failure notification; skipping email."
        )
        return
    business_name = (
        getattr(business, "businessName", None) if business else None
    ) or f"Business ID {business_id}"
    stripe_account_id = (
        getattr(business, "stripe_account_id", None) if business else None
    ) or "—"
    admin_url = f"{settings.FRONTEND_BASE_URL}/admin"
    error_kind = "Stripe API error" if stripe_error else "Processing error"
    context = {
        "business_id": business_id,
        "business_name": business_name,
        "stripe_account_id": stripe_account_id,
        "error_message": error_message or "Unknown error",
        "error_kind": error_kind,
        "admin_url": admin_url,
        "recipient_email": ", ".join(recipient_list),
    }
    send_templated_email(
        recipient_list=recipient_list,
        template_name="emails/super_admin_payout_failed.html",
        context=context,
        subject=f"[ClassEasily] Payout failed ({error_kind}): {business_name} (ID {business_id})",
    )
    logger.info(
        "Super Admin payout-failure email queued for business_id=%s to %s recipient(s).",
        business_id,
        len(recipient_list),
    )


def send_super_admin_booking_cancelled_email(booking: Booking):
    """Email all Super Admins when a booking is cancelled. Includes booking and booker info."""
    recipient_list = get_super_admin_emails()
    if not recipient_list:
        logger.debug("No Super Admin recipients for booking cancellation notification; skipping email.")
        return
    if not booking:
        logger.warning("send_super_admin_booking_cancelled_email called with no booking.")
        return
    related_data = _get_booking_related_data(booking)
    booker = _get_booker_display(booking)
    admin_url = f"{settings.FRONTEND_BASE_URL}/admin"
    context = {
        "booking": booking,
        "related_data": related_data,
        "booker": booker,
        "admin_url": admin_url,
        "recipient_email": ", ".join(recipient_list),
    }
    send_templated_email(
        recipient_list=recipient_list,
        template_name="emails/super_admin_booking_cancelled.html",
        context=context,
        subject=f"[ClassEasily] Booking Cancelled: {related_data.get('class_title', 'N/A')} – {booker['name']}",
    )
    logger.info(
        f"Super Admin booking-cancelled email prepared/queued for booking {booking.id} to {len(recipient_list)} Super Admin(s)."
    )


def send_review_submission_confirmation_email(user: CustomUser, review: Reviews):
    """Sends confirmation after a user submits a review."""
    if not user or not user.email or not review:
        logger.warning(
            "Attempted to send review confirmation with invalid user or review."
        )
        return

    class_name = "N/A"
    try:
        if review.classId and review.classId.title:
            class_name = review.classId.title
            logger.debug(
                f"Successfully fetched class_name '{class_name}' from review.classId.title for review {review.reviewId}"
            )
        else:
            logger.warning(
                f"review.classId or review.classId.title is missing for review {review.reviewId}. Using default '{class_name}'."
            )
    except AttributeError as e:
        logger.error(
            f"AttributeError getting class_name for review {review.reviewId}: {e}",
            exc_info=True,
        )

    logger.info(
        f"Preparing review submission confirmation email for review {review.reviewId} to user {user.email} with class_name='{class_name}'"
    )

    class_page_url = None
    try:
        cm = review.classId
        if cm:
            ident = getattr(cm, "slug", None) or getattr(cm, "classId", None)
            if ident:
                base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
                class_page_url = f"{base}/classes/{ident}" if base else f"/classes/{ident}"
    except Exception:
        class_page_url = None

    context = {
        "user": user,
        "review": review,
        "class_name": class_name,
        "class_page_url": class_page_url,
        "recipient_email": user.email,
    }

    subject_class_name = class_name if class_name != "N/A" else "[Class Name]"
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/review_submission_confirmation_user.html",
        context=context,
        subject=f"We Received Your Review for {subject_class_name}",
    )
    logger.info(
        f"Review submission confirmation prepared/queued for review {review.reviewId}"
    )


def send_review_response_notification_email(user: CustomUser, review: Reviews):
    """Notifies a user when a business responds to their review."""
    if not user or not user.email or not review:
        logger.warning(
            "Attempted to send review response notification with invalid user or review."
        )
        return

    business_name = "N/A"
    class_identifier = None
    class_name = "N/A"
    try:
        business_name = review.businessId.businessName
        class_identifier = review.classId.slug or review.classId.classId
        class_name = review.classId.title
    except AttributeError:
        logger.error(
            f"Could not access related data for review {review.reviewId} when sending response notification."
        )

    logger.info(
        f"Preparing review response notification email for review {review.reviewId} to user {user.email}"
    )

    review_url = (
        f"{settings.FRONTEND_BASE_URL}/classes/{class_identifier}"
        if class_identifier
        else "#"
    )

    context = {
        "user": user,
        "review": review,
        "business_name": business_name,
        "class_name": class_name,
        "review_url": review_url,
        "recipient_email": user.email,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/review_response_notification_user.html",
        context=context,
        subject=f"{business_name} Responded to Your Review",
    )
    logger.info(
        f"Review response notification prepared/queued for review {review.reviewId}"
    )


def send_support_ticket_created_email(user: CustomUser, ticket: SupportTicket):
    """Sends confirmation after a user creates a support ticket."""
    if not user or not user.email or not ticket:
        logger.warning(
            "Attempted to send ticket created confirmation with invalid user or ticket."
        )
        return

    logger.info(
        f"Preparing support ticket created confirmation email for ticket {ticket.ticket_id} to user {user.email}"
    )

    ticket_url = f"{settings.FRONTEND_BASE_URL}/my-tickets/{ticket.ticket_id}"

    context = {
        "user": user,
        "ticket": ticket,
        "ticket_url": ticket_url,
        "recipient_email": user.email,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/support_ticket_created_user.html",
        context=context,
        subject=f"Support Ticket Received [#{ticket.user_facing_id}] - {ticket.subject}",
    )
    logger.info(
        f"Support ticket created confirmation prepared/queued for ticket {ticket.ticket_id}"
    )


def send_agent_reply_email(
    user: CustomUser, ticket: SupportTicket, agent: Optional[CustomUser] = None
):
    """Notifies a user when a support agent replies to their ticket."""
    if not user or not user.email or not ticket:
        logger.warning(
            "Attempted to send agent reply notification with invalid user or ticket."
        )
        return

    agent_name = (
        agent.get_full_name() if agent and agent.get_full_name() else "A support agent"
    )

    logger.info(
        f"Preparing agent reply notification email for ticket {ticket.ticket_id} to user {user.email}"
    )

    ticket_url = f"{settings.FRONTEND_BASE_URL}/my-tickets/{ticket.ticket_id}"

    context = {
        "user": user,
        "ticket": ticket,
        "agent_name": agent_name,
        "ticket_url": ticket_url,
        "recipient_email": user.email,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/support_agent_reply_user.html",
        context=context,
        subject=f"Re: Support Ticket [#{ticket.user_facing_id}] - {ticket.subject}",
    )
    logger.info(
        f"Agent reply notification prepared/queued for ticket {ticket.ticket_id}"
    )


def send_ticket_resolved_email(user: CustomUser, ticket: SupportTicket):
    """Notifies a user when their support ticket is resolved."""
    if not user or not user.email or not ticket:
        logger.warning(
            "Attempted to send ticket resolved notification with invalid user or ticket."
        )
        return

    logger.info(
        f"Preparing ticket resolved notification email for ticket {ticket.ticket_id} to user {user.email}"
    )

    ticket_url = f"{settings.FRONTEND_BASE_URL}/my-tickets/{ticket.ticket_id}"

    context = {
        "user": user,
        "ticket": ticket,
        "ticket_url": ticket_url,
        "recipient_email": user.email,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/support_ticket_resolved_user.html",
        context=context,
        subject=f"Support Ticket Resolved [#{ticket.user_facing_id}] - {ticket.subject}",
    )
    logger.info(
        f"Ticket resolved notification prepared/queued for ticket {ticket.ticket_id}"
    )


def send_business_verification_approved_email(user: CustomUser, business: BusinessInfo):
    """Sends notification when a business verification is approved."""
    if not user or not user.email or not business:
        logger.warning(
            "Attempted to send verification approved email with invalid user or business."
        )
        return

    logger.info(
        f"Preparing verification approved email for business {business.businessId} to user {user.email}"
    )

    dashboard_url = f"{settings.FRONTEND_BASE_URL}/business/dashboard/overview"

    context = {
        "user": user,
        "business": business,
        "dashboard_url": dashboard_url,
        "recipient_email": user.email,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/business_verification_approved.html",
        context=context,
        subject=f"Your Business '{business.businessName}' is Verified!",
    )
    logger.info(
        f"Verification approved email prepared/queued for business {business.businessId}"
    )


def send_business_verification_rejected_email(
    user: CustomUser, business: BusinessInfo, verification_request: VerificationRequest
):
    """Sends notification when a business verification is rejected."""
    if not user or not user.email or not business or not verification_request:
        logger.warning(
            "Attempted to send verification rejected email with invalid user, business, or request."
        )
        return

    logger.info(
        f"Preparing verification rejected email for business {business.businessId} to user {user.email}"
    )

    contact_email = settings.NOTIFICATION_SETTINGS.get(
        "reply_to", "support@classeasily.com"
    )
    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    verification_settings_url = (
        f"{base}/business/dashboard/settings?tab=preferences"
        if base
        else "/business/dashboard/settings?tab=preferences"
    )

    context = {
        "user": user,
        "business": business,
        "verification_request": verification_request,
        "contact_email": contact_email,
        "recipient_email": user.email,
        "verification_settings_url": verification_settings_url,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/business_verification_rejected.html",
        context=context,
        subject=f"Action Required: Update for Your Business Verification",
    )
    logger.info(
        f"Verification rejected email prepared/queued for business {business.businessId}"
    )


def send_business_new_booking_email(business_user: CustomUser, booking: Booking):
    """Notifies a business user about a new booking."""
    if not business_user or not business_user.email or not booking:
        logger.warning(
            "Attempted to send new booking email with invalid recipient or booking."
        )
        return

    related_data = _get_booking_related_data(booking)
    if not related_data.get("class_title"):
        logger.error(
            f"Could not access related class data for booking {booking.id} when sending new booking email."
        )
        return

    logger.info(
        f"Preparing new booking notification email for booking {booking.id} to business user {business_user.email}"
    )

    dashboard_booking_url = (
        f"{settings.FRONTEND_BASE_URL}/business/dashboard/bookings/active?bookingId={booking.id}"
    )

    formatted_time_range = "N/A"
    formatted_duration_minutes = None
    if booking.schedule_instance:
        try:
            start_time = booking.schedule_instance.time
            duration_minutes = getattr(booking.schedule_instance, "duration", 0) or 0
            dummy_date = datetime.now().date()
            start_dt = datetime.combine(dummy_date, start_time)
            end_dt = start_dt + timedelta(minutes=duration_minutes)
            time_str_start = start_dt.strftime("%-I:%M %p")
            time_str_end = end_dt.strftime("%-I:%M %p")
            formatted_time_range = f"{time_str_start} – {time_str_end}"
            formatted_duration_minutes = f"{duration_minutes} min" if duration_minutes else None
        except Exception as e:
            logger.warning(
                f"Error formatting time for business new-booking email {booking.id}: {e}"
            )

    context = {
        "business_user": business_user,
        "booking": booking,
        "dashboard_booking_url": dashboard_booking_url,
        "recipient_email": business_user.email,
        "related_data": related_data,
        "formatted_time_range": formatted_time_range,
        "formatted_duration_minutes": formatted_duration_minutes,
        "booking_reference": getattr(booking, "user_facing_reference", None) or booking.id,
        "amount_paid": getattr(booking, "amount_paid", None),
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name="emails/business_new_booking.html",
        context=context,
        subject=f"New Booking Received for {related_data['class_title']}",
    )
    logger.info(
        f"New booking notification email prepared/queued for booking {booking.id}"
    )


def send_business_student_cancellation_email(
    business_user: CustomUser, booking: Booking
):
    """Notifies a business user when a student cancels a booking."""
    if not business_user or not business_user.email or not booking:
        logger.warning(
            "Attempted to send student cancellation email with invalid recipient or booking."
        )
        return

    related_data = _get_booking_related_data(booking)
    if not related_data.get("class_title"):
        logger.error(
            f"Could not access related class data for booking {booking.id} when sending student cancellation email."
        )
        return

    logger.info(
        f"Preparing student cancellation notification email for booking {booking.id} to business user {business_user.email}"
    )

    dashboard_booking_url = (
        f"{settings.FRONTEND_BASE_URL}/business/dashboard/bookings/history?bookingId={booking.id}"
    )

    context = {
        "business_user": business_user,
        "booking": booking,
        "dashboard_booking_url": dashboard_booking_url,
        "recipient_email": business_user.email,
        "related_data": related_data,
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name="emails/business_booking_cancelled_by_student.html",
        context=context,
        subject=f"Booking Cancelled by Student for {related_data['class_title']}",
    )
    logger.info(
        f"Student cancellation notification email prepared/queued for booking {booking.id}"
    )


def send_payout_initiated_email(business_user: CustomUser, payout: Payout):
    """Notifies a business user that a payout has been initiated."""
    if not business_user or not business_user.email or not payout:
        logger.warning(
            "Attempted to send payout initiated email with invalid user or payout."
        )
        return

    logger.info(
        f"Preparing payout initiated email for Payout {payout.id} to user {business_user.email}"
    )

    dashboard_url = f"{settings.FRONTEND_BASE_URL}/business/dashboard/payouts"
    context = {
        "user": business_user,
        "payout": payout,
        "dashboard_url": dashboard_url,
        "recipient_email": business_user.email,
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name="emails/business_payout_initiated.html",
        context=context,
        subject=f"Your Payout of ${payout.amount:.2f} is on its way!",
    )


def send_payout_connect_required_email(
    business_user: CustomUser,
    pending_amount,
    booking_count: int,
    currency: str = "CAD",
):
    """
    Notifies a business user that they have pending payout earnings but no
    connected Stripe account. Sent at most once every 3 days (enforced by caller).
    """
    if not business_user or not business_user.email:
        logger.warning(
            "Attempted to send payout connect required email with invalid user."
        )
        return

    logger.info(
        f"Preparing payout connect required email to {business_user.email} "
        f"(pending_amount={pending_amount}, booking_count={booking_count})"
    )

    dashboard_url = f"{settings.FRONTEND_BASE_URL}/business/dashboard/payouts"
    context = {
        "user": business_user,
        "pending_amount": pending_amount,
        "booking_count": booking_count,
        "currency": currency or "CAD",
        "dashboard_url": dashboard_url,
        "recipient_email": business_user.email,
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name="emails/business_payout_connect_required.html",
        context=context,
        subject="Connect your account to receive your payout",
    )


def send_payout_failed_email(business: BusinessInfo, error_message: str):
    """
    Notify the business owner that a payout could not be completed.
    Not used by the daily payout task (that emails Super Admins only); kept for optional manual use.
    """
    if not business:
        logger.warning("Attempted to send payout failed email with no business.")
        return
    owner = getattr(business, "owner", None)
    if not owner or not getattr(owner, "email", None):
        logger.warning(
            "Business %s has no owner email for payout failed notification.",
            business.businessId,
        )
        return
    dashboard_url = f"{settings.FRONTEND_BASE_URL}/business/dashboard/payouts"
    context = {
        "user": owner,
        "business_name": getattr(business, "businessName", "Your business"),
        "error_message": error_message or "Unknown error",
        "dashboard_url": dashboard_url,
        "recipient_email": owner.email,
    }
    send_templated_email(
        recipient_list=[owner.email],
        template_name="emails/business_payout_failed.html",
        context=context,
        subject="Payout could not be completed – please check your payout account",
    )


def format_addon_subscription_display_name(
    addon_type: str, stripe_price_id: Optional[str] = None
) -> str:
    """
    User-facing subscription title for billing lifecycle emails (widget add-ons).
    Email marketing tiers append plan_label from Stripe price when known.
    """
    from quickstart.services.email_marketing_config import price_id_to_tier

    if addon_type == ADDON_TYPE_EMAIL_MARKETING:
        base = "Email Marketing Add-on"
        tier = price_id_to_tier(stripe_price_id) if stripe_price_id else None
        label = (tier or {}).get("plan_label")
        if label:
            return f"{base} ({label})"
        return base
    if addon_type == ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING:
        return "Marketplace Email Branding Add-on"
    return "Add-on"


def format_addon_price_display(
    addon_type: str, stripe_price_id: Optional[str] = None
) -> Optional[str]:
    """Optional monthly list price line for add-on lifecycle emails (from tier config)."""
    from quickstart.services.email_marketing_config import price_id_to_tier

    if addon_type == ADDON_TYPE_EMAIL_MARKETING and stripe_price_id:
        tier = price_id_to_tier(stripe_price_id)
        if tier:
            price = tier.get("ui_monthly_price")
            if price is not None:
                return f"${price}/mo"
    return None


def send_widget_subscription_payment_failed_email(
    business_user: CustomUser,
    business_name: str,
    plan_name: str,
    settings_billing_url: str,
    grace_days: int = 7,
):
    """
    Notify the business owner that their widget subscription payment failed (e.g. card expired or declined).
    Asks them to update their payment method within the grace period to avoid interruption.
    """
    if not business_user or not business_user.email:
        logger.warning(
            "Attempted to send widget subscription payment failed email with invalid user."
        )
        return

    logger.info(
        f"Preparing widget subscription payment failed email to {business_user.email} "
        f"(business={business_name}, plan={plan_name})"
    )

    context = {
        "user": business_user,
        "business_name": business_name,
        "plan_name": plan_name,
        "settings_billing_url": settings_billing_url,
        "grace_days": grace_days,
        "recipient_email": business_user.email,
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name="emails/business_widget_subscription_payment_failed.html",
        context=context,
        subject="Action required: Update your payment method for your booking widget plan",
    )


def send_business_subscription_lifecycle_email(
    business_user: CustomUser,
    *,
    business_name: str,
    subscription_name: str,
    lifecycle_event: str,
    previous_plan: Optional[str] = None,
    current_plan: Optional[str] = None,
    effective_date: Optional[Any] = None,
    next_billing_date: Optional[Any] = None,
    manage_billing_url: Optional[str] = None,
    extra_message: Optional[str] = None,
    price_display: Optional[str] = None,
    cta_url: Optional[str] = None,
    cta_label: Optional[str] = None,
):
    """
    Generic lifecycle confirmations for business billing subscriptions (widget/add-ons).
    lifecycle_event supported values:
      - activated
      - plan_changed
      - cancellation_scheduled
      - reactivated
      - canceled
      - payment_failed
      - renewed
    """
    if not business_user or not business_user.email:
        logger.warning(
            "Attempted to send subscription lifecycle email with invalid business user."
        )
        return

    event_titles = {
        "activated": "Subscription Activated",
        "plan_changed": "Subscription Plan Updated",
        "cancellation_scheduled": "Subscription Cancellation Scheduled",
        "reactivated": "Subscription Reactivated",
        "canceled": "Subscription Ended",
        "payment_failed": "Subscription Payment Failed",
        "renewed": "Subscription Renewed",
    }
    event_subjects = {
        "activated": f"{subscription_name} is now active",
        "plan_changed": f"{subscription_name} plan updated",
        "cancellation_scheduled": f"{subscription_name} will cancel at period end",
        "reactivated": f"{subscription_name} has been reactivated",
        "canceled": f"{subscription_name} has ended",
        "payment_failed": f"Action required: payment failed for {subscription_name}",
        "renewed": f"{subscription_name} renewed successfully",
    }

    event_title = event_titles.get(lifecycle_event, "Subscription Update")
    subject = event_subjects.get(
        lifecycle_event, f"{subscription_name} update for {business_name}"
    )
    base = (settings.FRONTEND_BASE_URL or "").rstrip("/")
    billing_url = manage_billing_url or f"{base}/business/dashboard?tab=settings"
    dashboard_url = f"{base}/business/dashboard"
    default_cta_labels = {
        "activated": "Go to Dashboard",
        "plan_changed": "Manage billing",
        "cancellation_scheduled": "Reactivate subscription",
        "reactivated": "Go to Dashboard",
        "canceled": "Resubscribe",
        "payment_failed": "Update payment method",
        "renewed": "View billing",
    }
    default_cta_urls = {
        "activated": dashboard_url,
        "plan_changed": billing_url,
        "cancellation_scheduled": billing_url,
        "reactivated": dashboard_url,
        "canceled": billing_url,
        "payment_failed": billing_url,
        "renewed": billing_url,
    }
    resolved_cta_url = cta_url or default_cta_urls.get(lifecycle_event, billing_url)
    resolved_cta_label = cta_label or default_cta_labels.get(
        lifecycle_event, "Manage billing"
    )

    effective_date_display = None
    if effective_date:
        if hasattr(effective_date, "strftime"):
            effective_date_display = effective_date.strftime("%b %d, %Y")
        else:
            effective_date_display = str(effective_date)

    next_billing_date_display = None
    if next_billing_date:
        if hasattr(next_billing_date, "strftime"):
            next_billing_date_display = next_billing_date.strftime("%b %d, %Y")
        else:
            next_billing_date_display = str(next_billing_date)

    context = {
        "user": business_user,
        "recipient_email": business_user.email,
        "business_name": business_name or "Your business",
        "subscription_name": subscription_name,
        "event_title": event_title,
        "lifecycle_event": lifecycle_event,
        "previous_plan": previous_plan,
        "current_plan": current_plan,
        "effective_date": effective_date_display,
        "next_billing_date": next_billing_date_display,
        "manage_billing_url": billing_url,
        "cta_url": resolved_cta_url,
        "cta_label": resolved_cta_label,
        "extra_message": extra_message or "",
        "price_display": price_display or "",
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name="emails/business_subscription_lifecycle_confirmation.html",
        context=context,
        subject=subject,
    )


def _get_membership_member_contact(membership: CustomerMembership):
    email = None
    name = "there"
    if getattr(membership, "contact", None):
        c = membership.contact
        email = getattr(c, "email", None)
        name_parts = [
            n for n in [getattr(c, "first_name", ""), getattr(c, "last_name", "")]
            if n
        ]
        if name_parts:
            name = " ".join(name_parts)
    if (not email) and getattr(membership, "user", None):
        u = membership.user
        email = getattr(u, "email", None)
        full_name = u.get_full_name() if hasattr(u, "get_full_name") else ""
        name = full_name or email or name
    return email, name


def send_membership_lifecycle_member_email(
    membership: CustomerMembership,
    *,
    lifecycle_event: str,
    payment_url: Optional[str] = None,
    amount: Optional[Any] = None,
    currency: Optional[str] = None,
    extra_message: Optional[str] = None,
):
    """
    Membership lifecycle email to the member (contact/user).
    lifecycle_event supported values:
      - activated
      - renewed
      - payment_failed
      - approval_payment_required
      - cancellation_scheduled
      - reactivated
      - canceled
    """
    if not membership or not getattr(membership, "product", None):
        logger.warning(
            "Attempted to send membership lifecycle member email with invalid membership."
        )
        return
    to_email, member_name = _get_membership_member_contact(membership)
    if not to_email:
        logger.warning(
            "Membership %s has no member email for lifecycle notification.",
            getattr(membership, "id", "unknown"),
        )
        return

    product = membership.product
    business = product.business
    event_titles = {
        "activated": "Your Membership Is Active",
        "renewed": "Your Membership Renewed Successfully",
        "payment_failed": "Action Required: Membership Payment Failed",
        "approval_payment_required": "Membership Approved - Payment Required",
        "cancellation_scheduled": "Your Membership Will End at Period End",
        "reactivated": "Your Membership Has Been Reactivated",
        "canceled": "Your Membership Has Ended",
    }
    subjects = {
        "activated": f"You're in! {product.name} is active",
        "renewed": f"{product.name} renewed successfully",
        "payment_failed": f"Payment failed for {product.name}",
        "approval_payment_required": f"{product.name} approved - complete payment",
        "cancellation_scheduled": f"{product.name} will end at period end",
        "reactivated": f"{product.name} has been reactivated",
        "canceled": f"{product.name} has ended",
    }

    context = {
        "recipient_email": to_email,
        "member_name": member_name,
        "business_name": getattr(business, "businessName", "ClassEasily Business"),
        "membership_name": product.name,
        "event_title": event_titles.get(lifecycle_event, "Membership Update"),
        "lifecycle_event": lifecycle_event,
        "current_period_end": (
            membership.current_period_end.strftime("%b %d, %Y")
            if getattr(membership, "current_period_end", None)
            else None
        ),
        "payment_url": payment_url,
        "amount": amount,
        "currency": (currency or product.currency or "CAD").upper(),
        "extra_message": extra_message or "",
    }
    send_templated_email(
        recipient_list=[to_email],
        template_name="emails/membership_lifecycle_member.html",
        context=context,
        subject=subjects.get(lifecycle_event, f"{product.name} membership update"),
    )


def send_membership_lifecycle_business_email(
    membership: CustomerMembership,
    *,
    lifecycle_event: str,
    amount: Optional[Any] = None,
    currency: Optional[str] = None,
    extra_message: Optional[str] = None,
):
    """
    Membership lifecycle email to the business owner.
    """
    if not membership or not getattr(membership, "product", None):
        logger.warning(
            "Attempted to send membership lifecycle business email with invalid membership."
        )
        return
    business = membership.product.business
    owner = getattr(business, "owner", None)
    if not owner or not getattr(owner, "email", None):
        logger.warning(
            "Business %s has no owner email for membership lifecycle notification.",
            getattr(business, "businessId", "unknown"),
        )
        return
    member_email, member_name = _get_membership_member_contact(membership)
    event_titles = {
        "activated": "New Membership Activated",
        "renewed": "Membership Renewed",
        "payment_failed": "Membership Payment Failed",
        "approval_payment_required": "Membership Approved (Awaiting Payment)",
        "cancellation_scheduled": "Member Scheduled Cancellation",
        "reactivated": "Membership Reactivated",
        "canceled": "Membership Canceled",
    }
    subjects = {
        "activated": f"New active member: {membership.product.name}",
        "renewed": f"Membership renewed: {membership.product.name}",
        "payment_failed": f"Membership payment failed: {membership.product.name}",
        "approval_payment_required": f"Approved member awaiting payment: {membership.product.name}",
        "cancellation_scheduled": f"Cancellation scheduled: {membership.product.name}",
        "reactivated": f"Membership reactivated: {membership.product.name}",
        "canceled": f"Membership canceled: {membership.product.name}",
    }
    context = {
        "recipient_email": owner.email,
        "business_user": owner,
        "business_name": getattr(business, "businessName", "Your business"),
        "member_name": member_name or "Member",
        "member_email": member_email or "N/A",
        "membership_name": membership.product.name,
        "event_title": event_titles.get(lifecycle_event, "Membership Update"),
        "lifecycle_event": lifecycle_event,
        "current_period_end": (
            membership.current_period_end.strftime("%b %d, %Y")
            if getattr(membership, "current_period_end", None)
            else None
        ),
        "amount": amount,
        "currency": (currency or membership.product.currency or "CAD").upper(),
        "extra_message": extra_message or "",
        "members_dashboard_url": f"{settings.FRONTEND_BASE_URL}/business/dashboard/memberships",
    }
    send_templated_email(
        recipient_list=[owner.email],
        template_name="emails/membership_lifecycle_business.html",
        context=context,
        subject=subjects.get(lifecycle_event, "Membership lifecycle update"),
    )


def send_performance_summary_email(
    business_user: CustomUser, summary_data: dict, period: str
):
    """Sends a weekly/monthly performance summary to a business user."""
    if not business_user or not business_user.email:
        logger.warning("Attempted to send performance summary with invalid user.")
        return

    logger.info(
        f"Preparing {period} performance summary for user {business_user.email}"
    )

    dashboard_url = f"{settings.FRONTEND_BASE_URL}/business/dashboard/trends"
    context = {
        "user": business_user,
        "summary": summary_data,
        "period": period.title(),
        "dashboard_url": dashboard_url,
        "recipient_email": business_user.email,
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name="emails/business_performance_summary.html",
        context=context,
        subject=f"Your {period.title()} Performance Summary from ClassEasily",
    )


def send_concierge_handover_email(user: CustomUser, claim_url: str):
    """
    Sends the account claim email to a user who was onboarded via Concierge services.
    """
    recipient = (getattr(user, "email", None) or "").strip()
    if not user or not recipient:
        logger.warning("Attempted to send handover email to invalid user.")
        return

    logger.info(f"Preparing concierge handover email for user {recipient}")

    context = {
        "user": user,
        "claim_url": claim_url,
        "recipient_email": recipient,
    }

    send_templated_email(
        recipient_list=[recipient],
        template_name="emails/concierge_handover.html",
        context=context,
        subject="Your Business Account is Ready! - ClassEasily",
    )


def send_request_for_review_email(user: CustomUser, booking: Booking):
    """Sends a request for review 24 hours after a class is completed."""
    if not user or not user.email or not booking:
        logger.warning(
            "Attempted to send review request email with invalid user or booking."
        )
        return

    if Reviews.objects.filter(booking=booking).exists():
        logger.info(
            f"Skipping review request for booking {booking.id}, review already exists."
        )
        return

    logger.info(
        f"Preparing review request email for booking {booking.id} to user {user.email}"
    )

    related_data = _get_booking_related_data(booking)
    # Updated: Link to completed tab and highlight the booking to review
    review_url = (
        f"{settings.FRONTEND_BASE_URL}/my-classes?tab=completed&highlight={booking.id}"
    )

    context = {
        "user": user,
        "booking": booking,
        "related_data": related_data,
        "review_url": review_url,
        "recipient_email": user.email,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/user_request_for_review.html",
        context=context,
        subject=f"How was your '{related_data.get('class_title', 'class')}' experience?",
    )


def send_favorited_class_new_dates_email(
    user: CustomUser, class_main: ClassesMain, new_schedule: Schedule
):
    """Notifies a user that a class they favorited has new dates."""
    if not user or not user.email or not class_main:
        logger.warning(
            "Attempted to send favorite class new dates email with invalid data."
        )
        return

    logger.info(
        f"Preparing favorite class new dates email for class {class_main.classId} to user {user.email}"
    )

    class_identifier = class_main.slug or class_main.classId
    class_url = f"{settings.FRONTEND_BASE_URL}/classes/{class_identifier}"
    context = {
        "user": user,
        "class_main": class_main,
        "new_schedule": new_schedule,
        "class_url": class_url,
        "recipient_email": user.email,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/user_favorited_class_new_dates.html",
        context=context,
        subject=f"New dates are available for {class_main.title}!",
    )


def send_admin_user_reply_notification(recipients: List[str], ticket: SupportTicket):
    """Notifies admins that a user has replied to a support ticket."""
    if not recipients:
        logger.warning(
            f"No admin recipients for user reply on ticket {ticket.ticket_id}."
        )
        return

    logger.info(
        f"Preparing admin notification for user reply on ticket {ticket.ticket_id}"
    )

    ticket_url = (
        f"{settings.FRONTEND_BASE_URL}/admin/support?ticket={ticket.ticket_id}"
    )
    context = {
        "ticket": ticket,
        "user_name": ticket.user.get_full_name() or ticket.user.email,
        "ticket_url": ticket_url,
    }
    send_templated_email(
        recipient_list=recipients,
        template_name="emails/admin_user_support_reply.html",
        context=context,
        subject=f"[User Reply] Ticket #{ticket.user_facing_id} - {ticket.subject}",
    )


def send_booking_rescheduled_by_business_email(
    user,
    booking: Booking,
    old_instance: ScheduleInstance,
    new_instance: ScheduleInstance,
    booking_source=None,
):
    """
    Notifies a user that their booking was rescheduled by the business.
    booking_source: optional widget / member_widget / marketplace for branding resolution.
    """
    if not user or not user.email or not booking:
        logger.warning(
            "Attempted to send booking rescheduled email with invalid user or booking."
        )
        return

    related_data = _get_booking_related_data(booking)
    if not related_data.get("class_title"):
        logger.error(
            f"Could not access related data for booking {booking.id} when sending reschedule notification."
        )

    logger.info(
        f"Preparing booking rescheduled email for booking {booking.id} to user {user.email}"
    )

    manage_bookings_url = (
        f"{settings.FRONTEND_BASE_URL}/my-classes?tab=upcoming&highlight={booking.id}"
    )

    # Calculate End Time for the NEW instance
    new_end_time = None
    try:
        dummy_date = datetime.now().date()
        start_dt = datetime.combine(dummy_date, new_instance.time)
        end_dt = start_dt + timedelta(minutes=new_instance.duration)
        new_end_time = end_dt.time()
    except Exception as e:
        logger.error(f"Error calculating new end time for reschedule email: {e}")
        new_end_time = new_instance.time  # Fallback to start time to prevent crash

    # NEW: Format timezone (Replace underscore with space)
    formatted_timezone = related_data.get("business_timezone", "UTC")
    if formatted_timezone:
        formatted_timezone = formatted_timezone.replace("_", " ")

    context = {
        "user": user,
        "booking": booking,
        "old_instance": old_instance,
        "new_instance": new_instance,
        "new_end_time": new_end_time,
        "formatted_timezone": formatted_timezone,  # Added to context
        "manage_bookings_url": manage_bookings_url,
        "recipient_email": user.email,
        "related_data": related_data,
    }

    if booking.schedule_instance:
        _attach_booking_email_branding(context, booking, booking_source)

    subject_line = f"Update: Your Booking for {related_data.get('class_title', '[Class Title]')} Has Been Rescheduled"
    if _try_send_custom_branding_html_email(
        [user.email],
        EMAIL_TYPE_BOOKING_RESCHEDULED,
        context,
        subject_line,
        attachments=None,
    ):
        logger.info(
            f"Booking reschedule custom HTML email prepared/queued for booking {booking.id}"
        )
        return

    send_templated_email(
        recipient_list=[user.email],
        template_name="emails/booking_rescheduled_by_business.html",
        context=context,
        subject=subject_line,
    )
    logger.info(f"Booking reschedule email prepared/queued for booking {booking.id}")


# --- Guest–Business messaging notification emails (with cooldown) ---
MESSAGE_EMAIL_COOLDOWN_MINUTES = 15


def _conversation_should_send_email(conversation, recipient_side):
    """
    Check if we should send a new-reply email (first message to this side, or past cooldown).
    If we send, caller must call _conversation_record_email_sent after sending.
    """
    log = ConversationEmailLog.objects.filter(
        conversation=conversation, recipient_side=recipient_side
    ).first()
    if not log:
        return True  # First time → send
    threshold = timezone.now() - timedelta(minutes=MESSAGE_EMAIL_COOLDOWN_MINUTES)
    return log.last_email_sent_at < threshold


def _conversation_record_email_sent(conversation, recipient_side):
    """Update or create ConversationEmailLog after sending."""
    now = timezone.now()
    ConversationEmailLog.objects.update_or_create(
        conversation=conversation,
        recipient_side=recipient_side,
        defaults={"last_email_sent_at": now},
    )


def send_guest_message_notification_email(conversation: Conversation, message: ConversationMessage):
    """
    Notify business (owner + staff with receive_booking_notifications) when a booker sends a message.
    Respects cooldown per conversation for business side.
    """
    if not conversation or not message or message.sender_type != ConversationMessage.SENDER_BOOKER:
        return
    if not _conversation_should_send_email(conversation, ConversationEmailLog.RECIPIENT_BUSINESS):
        logger.info(
            f"Skipping guest-message notification for conversation {conversation.id} (cooldown)."
        )
        return
    business = conversation.business
    recipients = {business.owner}
    staff_with_notifications = BusinessStaff.objects.filter(
        business=business,
        status="accepted",
        role__permissions__codename="receive_booking_notifications",
    ).select_related("user")
    for staff in staff_with_notifications:
        if staff.user and staff.user.email:
            recipients.add(staff.user)
    messages_url = f"{settings.FRONTEND_BASE_URL}/business/dashboard/messages"
    context = {
        "conversation": conversation,
        "message": message,
        "business": business,
        "messages_url": messages_url,
        "booker_display": None,
    }
    if conversation.booker_user:
        context["booker_display"] = (
            conversation.booker_user.get_full_name() or conversation.booker_user.email
        )
    elif conversation.booker_contact:
        c = conversation.booker_contact
        context["booker_display"] = f"{c.first_name} {c.last_name}".strip() or (c.email or "Guest")
    sent_any = False
    for recipient in recipients:
        if not recipient or not recipient.email:
            continue
        context["recipient_email"] = recipient.email
        send_templated_email(
            recipient_list=[recipient.email],
            template_name="emails/guest_new_message_business.html",
            context=context,
            subject=f"New message from a guest – {business.businessName}",
        )
        sent_any = True
    if sent_any:
        _conversation_record_email_sent(conversation, ConversationEmailLog.RECIPIENT_BUSINESS)
        logger.info(f"Guest message notification sent for conversation {conversation.id}")


def send_business_reply_notification_email(conversation: Conversation, message: ConversationMessage):
    """
    Notify booker (user or contact email) when business sends a reply.
    Respects cooldown per conversation for booker side.
    """
    if not conversation or not message or message.sender_type != ConversationMessage.SENDER_BUSINESS:
        return
    if not _conversation_should_send_email(conversation, ConversationEmailLog.RECIPIENT_BOOKER):
        logger.info(
            f"Skipping business-reply notification for conversation {conversation.id} (cooldown)."
        )
        return
    to_email = None
    booker_display = "there"
    if conversation.booker_user:
        to_email = conversation.booker_user.email
        booker_display = (
            conversation.booker_user.get_full_name() or conversation.booker_user.email or "there"
        )
    elif conversation.booker_contact:
        to_email = conversation.booker_contact.email
        c = conversation.booker_contact
        booker_display = f"{c.first_name} {c.last_name}".strip() or (c.email or "there")
    if not to_email:
        logger.warning(f"Cannot send business-reply notification: no booker email for conversation {conversation.id}")
        return
    # Guest (no account) gets a magic link; logged-in bookers go to my-messages
    if conversation.booker_contact:
        from quickstart.utils.guest_inbox_token import create_guest_inbox_token
        token = create_guest_inbox_token(str(conversation.id), str(conversation.booker_contact_id))
        messages_url = f"{settings.FRONTEND_BASE_URL}/?guest_inbox_token={token}"
    else:
        messages_url = f"{settings.FRONTEND_BASE_URL}/?conversation_id={conversation.id}"
    context = {
        "conversation": conversation,
        "message": message,
        "business": conversation.business,
        "messages_url": messages_url,
        "booker_display": booker_display,
        "recipient_email": to_email,
    }
    send_templated_email(
        recipient_list=[to_email],
        template_name="emails/business_reply_to_guest.html",
        context=context,
        subject=f"New reply from {conversation.business.businessName}",
    )
    _conversation_record_email_sent(conversation, ConversationEmailLog.RECIPIENT_BOOKER)
    logger.info(f"Business reply notification sent for conversation {conversation.id}")
