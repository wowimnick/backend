from datetime import datetime, timedelta
import logging
from icalendar import Calendar, Event, vRecur, vText
from django.core.mail import EmailMultiAlternatives # Keep for type hints maybe
from django.template.loader import render_to_string
from django.conf import settings
from django.utils import timezone
from typing import Any, Dict, List, Optional
import pytz
import logging

logger = logging.getLogger(__name__)
try:
    from ..tasks import send_email_task
    CELERY_ENABLED = True
except ImportError:
    logger.error("Celery task 'send_email_task' not found. Email sending will be logged only.")
    CELERY_ENABLED = False
    # Define a dummy delay method if celery isn't installed/configured properly
    class DummyTask:
        def delay(self, *args, **kwargs):
            logger.warning("Celery not configured. send_email_task.delay called but task not queued.")
            pass
    send_email_task = DummyTask()

from ..models import Booking, CustomUser, Reviews, SupportTicket, BusinessInfo, VerificationRequest

logger = logging.getLogger(__name__)

# --- Helper function to safely get related data (ensure business_timezone is included) ---
def _get_booking_related_data(booking: Booking) -> dict:
    data = {
        'class_title': "N/A",
        'class_id': None,
        'business_name': "N/A",
        'option_title': "N/A",
        'class_location': "N/A",
        'business_timezone': 'UTC', # Default
        'business_contact_email': settings.DEFAULT_FROM_EMAIL, # Default
    }
    try:
        schedule_instance = getattr(booking, 'schedule_instance', None)
        if not schedule_instance: raise AttributeError("schedule_instance missing")
        schedule = getattr(schedule_instance, 'schedule', None)
        if not schedule: raise AttributeError("schedule missing")
        option = getattr(schedule, 'option', None)
        if not option: raise AttributeError("option missing")
        class_main = getattr(option, 'classId', None)
        if not class_main: raise AttributeError("classId missing from option")
        business = getattr(class_main, 'businessId', None)
        if not business: raise AttributeError("businessId missing from class_main")

        data['class_title'] = getattr(class_main, 'title', "N/A")
        data['class_id'] = getattr(class_main, 'classId', None)
        data['business_name'] = getattr(business, 'businessName', "N/A")
        data['option_title'] = getattr(option, 'title', "N/A")
        data['class_location'] = getattr(class_main, 'location', "N/A")
        data['business_timezone'] = getattr(business, 'business_timezone', 'UTC')
        data['business_contact_email'] = getattr(business, 'studentContactEmail', settings.DEFAULT_FROM_EMAIL)

    except AttributeError as e:
        logger.error(f"Could not access related data for booking {booking.id} when preparing email context: {e}", exc_info=True)
    return data

# --- Helper to generate ICS content ---
def _generate_ics_content(booking: Booking, related_data: Dict[str, Any], user: CustomUser) -> Optional[str]:
    if not booking.schedule_instance:
        logger.warning(f"Cannot generate ICS for booking {booking.id}: schedule_instance is missing.")
        return None

    try:
        cal = Calendar()
        cal.add('prodid', f'-//ClassEasily Booking//classeasily.com//EN')
        cal.add('version', '2.0')
        cal.add('method', 'REQUEST') # For calendar invites, or PUBLISH for just event data

        event = Event()
        class_title = related_data.get('class_title', 'Class Booking')
        option_title = related_data.get('option_title', '')
        event_summary = f"{class_title}{f' - {option_title}' if option_title and option_title != class_title else ''}"
        event.add('summary', vText(event_summary))

        business_timezone_str = related_data.get('business_timezone', 'UTC')
        try:
            business_tz = pytz.timezone(business_timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.error(f"Unknown business timezone '{business_timezone_str}' for booking {booking.id}. Defaulting to UTC for ICS.")
            business_tz = pytz.utc
        
        schedule_inst = booking.schedule_instance
        naive_start_dt = datetime.combine(schedule_inst.date, schedule_inst.time)
        aware_start_business = business_tz.localize(naive_start_dt)
        
        # For ICS, DTSTART and DTEND should ideally be in UTC or have TZID specified
        # Using UTC is generally safer for broader compatibility.
        aware_start_utc = aware_start_business.astimezone(pytz.utc)
        aware_end_utc = aware_start_utc + timedelta(minutes=schedule_inst.duration)

        event.add('dtstart', aware_start_utc)
        event.add('dtend', aware_end_utc)
        event.add('dtstamp', datetime.utcnow().replace(tzinfo=pytz.utc)) # Timestamp of ICS creation
        
        # Unique ID for the event
        uid_domain = settings.SITE_DOMAIN or "classeasily.com" # Get from settings or default
        event.add('uid', f'{booking.user_facing_reference or booking.id}-{schedule_inst.date.strftime("%Y%m%d")}@{uid_domain}')
        
        event.add('location', vText(related_data.get('class_location', 'N/A')))
        
        description_parts = [
            f"Your booking for: {event_summary}",
            f"Business: {related_data.get('business_name', 'N/A')}",
            f"Reference: {booking.user_facing_reference or f'ID #{booking.id}'}",
            f"Participants: {booking.participants}",
        ]
        if booking.notes:
            description_parts.append(f"Your Notes: {booking.notes}")
        event.add('description', vText("\n".join(description_parts)))

        # Organizer
        organizer_email = related_data.get('business_contact_email', settings.DEFAULT_FROM_EMAIL)
        event.add('organizer', f'MAILTO:{organizer_email}', parameters={'CN': vText(related_data.get('business_name', 'ClassEasily Business'))})

        # Attendee (the user)
        if user and user.email:
            attendee_cn = vText(user.get_full_name() or user.email)
            event.add('attendee', f'MAILTO:{user.email}', parameters={'CN': attendee_cn, 'ROLE': 'REQ-PARTICIPANT', 'PARTSTAT': 'NEEDS-ACTION', 'RSVP': 'TRUE'})
        
        event.add('status', 'CONFIRMED')
        event.add('transp', 'OPAQUE') # Shows as busy time

        # Recurrence for courses
        if booking.enrollment_type == 'Full Course' and booking.booking_group_id:
            pass # No RRULE for single instance ICS. Add if this email is for the *entire* course.


        cal.add_component(event)
        return cal.to_ical().decode('utf-8')

    except Exception as e:
        logger.error(f"Failed to generate ICS content for booking {booking.id}: {e}", exc_info=True)
        return None

# --- Main Function to Send Templated Emails (NOW USES TASK QUEUE) ---

def send_templated_email(recipient_list, template_name, context, subject=None, attachments=None): # Added attachments
    if not recipient_list:
        logger.warning("No recipients provided for template %s, skipping email queuing.", template_name)
        return

    context.setdefault('recipient_email', recipient_list[0] if recipient_list else "N/A")
    context.setdefault('frontend_base_url', settings.FRONTEND_BASE_URL)
    context.setdefault('settings', settings)

    try:
        html_content = render_to_string(template_name, context)
    except Exception as e:
        logger.error(f"Error rendering email template '{template_name}' with context keys {list(context.keys())}: {e}", exc_info=True)
        return

    if not subject:
        try:
            from django.template import Template, Context
            minimal_template_string = "{% extends '" + template_name + "' %}{% block title %}{% endblock %}"
            template_obj = Template(minimal_template_string)
            subject = template_obj.render(Context(context)).strip()
            if not subject:
                 subject = "Notification from ClassEasily"
        except Exception as e:
            logger.warning(f"Could not derive subject from template '{template_name}': {e}. Using default.")
            subject = "Notification from ClassEasily"
    subject = subject or "Notification from ClassEasily"

    try:
        task_kwargs = {
            'subject': subject,
            'body': "Please view this email in an HTML-compatible client.", # Basic plain text body
            'from_email': settings.DEFAULT_FROM_EMAIL,
            'to_list': recipient_list,
            'reply_to_list': [settings.NOTIFICATION_SETTINGS.get('reply_to')] if settings.NOTIFICATION_SETTINGS.get('reply_to') else None,
            'html_content': html_content,
            'attachments': attachments # Pass attachments to the task
        }
        send_email_task.delay(**task_kwargs)
        logger.info(f"Email task queued for {recipient_list} using template '{template_name}'. Subject: '{subject}'. Attachments: {'Yes' if attachments else 'No'}")
    except Exception as e:
        logger.error(f"Error queuing email task for template '{template_name}' to {recipient_list}: {e}", exc_info=True)

# --- Specific Email Trigger Functions ---

def send_welcome_email(user):
    """Sends the welcome email to a newly registered user."""
    if not user or not user.email:
        logger.warning("Attempted to send welcome email to invalid user.")
        return
    logger.info(f"Preparing welcome email for user {user.email}")
    context = {
        'user': user,
        'explore_url': f"{settings.FRONTEND_BASE_URL}/explore",
        'recipient_email': user.email, # Explicitly set
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/welcome_user.html',
        context=context
        # Subject derived from template
    )

def send_account_security_email(user, change_type, new_email=None, subject=None): # Added subject param
    """
    Sends a security notification after password or email change.

    Args:
        user (CustomUser): The user object.
        change_type (str): "password" or "email_update" or "email_added".
        new_email (str, optional): The new email address (if change_type involves new email).
        subject (str, optional): Explicit subject line.
    """
    if not user or not user.email:
        logger.warning("Attempted to send security email to invalid user.")
        return

    # Determine the recipient address carefully
    # For password changes or adding secondary, notify primary
    # For email *updates*, notify the *new* primary email address
    recipient = user.email # Default to current user email
    # If the user's email itself was just updated, the 'user' object might
    # already reflect the NEW email. Confirm this based on where it's called.
    # The allauth signal provides both old and new, which is helpful.

    logger.info(f"Preparing security email (type: {change_type}) for user {user.userId} to {recipient}")

    context = {
        'user': user,
        'change_type': change_type,
        'change_time': timezone.now(), # Use current time for notification
        'recipient_email': recipient, # Pass for footer context
        'new_email_address': new_email # Pass new email if type involves it
    }
    send_templated_email(
        recipient_list=[recipient],
        template_name='emails/account_security_change.html',
        context=context,
        subject=subject # Pass explicit subject
    )

def send_booking_confirmation_email(user: CustomUser, booking: Booking):
    if not user or not user.email or not booking:
        logger.warning("Attempted to send booking confirmation with invalid user or booking.")
        return

    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_id'):
        logger.error(f"Could not access essential related data for booking {booking.id} when sending confirmation.")

    logger.info(f"Preparing booking confirmation email for booking {booking.id} to user {user.email}")

    class_details_url = f"{settings.FRONTEND_BASE_URL}/classes/{related_data.get('class_id', '')}" if related_data.get('class_id') else "#"
    manage_bookings_url = f"{settings.FRONTEND_BASE_URL}/my-classes"
    
    payment = booking.payments.filter(status='succeeded').order_by('-created_at').first()

    context = {
        'user': user,
        'booking': booking, 
        'class_details_url': class_details_url,
        'manage_bookings_url': manage_bookings_url,
        'recipient_email': user.email,
        'related_data': related_data,
        'payment': payment 
    }

    # Generate ICS content
    ics_content_str = _generate_ics_content(booking, related_data, user)
    email_attachments = None
    if ics_content_str:
        filename_class_part = (related_data.get('class_title', 'class')[:20]).replace(' ', '_').replace('/', '_')
        ics_filename = f"{filename_class_part}_booking_{booking.schedule_instance.date.strftime('%Y%m%d')}.ics"
        email_attachments = [{
            'filename': ics_filename,
            'content': ics_content_str, # Pass as string
            'mimetype': 'text/calendar; charset=utf-8; method=REQUEST' # Method can be PUBLISH or REQUEST
        }]
        logger.info(f"Generated ICS attachment: {ics_filename} for booking {booking.id}")
    else:
        logger.warning(f"Could not generate ICS attachment for booking {booking.id}")

    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/booking_confirmation_user.html',
        context=context,
        subject=f"Your Booking for {related_data.get('class_title', '[Class Title]')} is Confirmed!",
        attachments=email_attachments # Pass the attachments list
    )

def send_booking_cancellation_user_email(user: CustomUser, booking: Booking, refund_details: str):
    """
    Sends confirmation to a user after they cancelled their booking.
    """
    if not user or not user.email or not booking:
        logger.warning("Attempted to send user booking cancellation email with invalid user or booking.")
        return

    related_data = _get_booking_related_data(booking)
    # Log error if class_title is missing but proceed with sending
    if not related_data.get('class_title'):
        logger.error(f"Could not access class_title for booking {booking.id} when sending user cancellation email.")
    
    logger.info(f"Preparing user booking cancellation email for booking {booking.id} to user {user.email}")

    explore_url = f"{settings.FRONTEND_BASE_URL}/explore"

    # The 'booking' object itself contains booking.participants and booking.participant_details
    context = {
        'user': user,
        'booking': booking,
        'refund_details': refund_details,
        'explore_url': explore_url,
        'recipient_email': user.email,
        'related_data': related_data,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/booking_cancellation_user.html',
        context=context,
        subject=f"Your Booking for {related_data.get('class_title', '[Class Title]')} Has Been Cancelled"
    )
    logger.info(f"User booking cancellation email prepared/queued for booking {booking.id}")


def send_booking_cancelled_by_other_email(user: CustomUser, booking: Booking, cancelled_by: str, reason: str, contact_info: str):
    """
    Sends notification to a user when their booking is cancelled by the business or an admin.
    """
    if not user or not user.email or not booking:
        logger.warning("Attempted to send 'cancelled by other' email with invalid user or booking.")
        return

    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_title'):
        logger.error(f"Could not access related data for booking {booking.id} when sending 'cancelled by other' email.")
        # Proceed with sending, template handles defaults
    
    logger.info(f"Preparing 'cancelled by other' email for booking {booking.id} to user {user.email}")

    explore_url = f"{settings.FRONTEND_BASE_URL}/explore"

    # The 'booking' object itself contains booking.participants and booking.participant_details
    context = {
        'user': user,
        'booking': booking,
        'cancelled_by': cancelled_by,
        'reason': reason or "No specific reason provided.",
        'contact_info': contact_info,
        'explore_url': explore_url,
        'recipient_email': user.email,
        'related_data': related_data,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/booking_cancelled_by_other.html',
        context=context,
        subject=f"Update: Your Booking for {related_data.get('class_title', '[Class Title]')} Was Cancelled"
    )
    logger.info(f"'Cancelled by other' email prepared/queued for booking {booking.id}")


def send_booking_reminder_email(user: CustomUser, booking: Booking):
    """
    Sends a reminder email to a user about an upcoming class.
    """
    if not user or not user.email or not booking:
        logger.warning("Attempted to send booking reminder with invalid user or booking.")
        return

    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_id'):
        logger.error(f"Could not access class_id for booking {booking.id} when sending reminder email.")
        # Proceed, template and URL construction will handle missing class_id

    logger.info(f"Preparing booking reminder email for booking {booking.id} to user {user.email}")

    class_details_url = f"{settings.FRONTEND_BASE_URL}/classes/{related_data.get('class_id', '')}" if related_data.get('class_id') else "#"

    # The 'booking' object itself contains booking.participants and booking.participant_details
    context = {
        'user': user,
        'booking': booking,
        'class_details_url': class_details_url,
        'recipient_email': user.email,
        'related_data': related_data,
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/booking_reminder_user.html',
        context=context,
        subject=f"Reminder: Your Class '{related_data.get('class_title', '[Class Title]')}' is Soon!"
    )
    logger.info(f"Booking reminder email prepared/queued for booking {booking.id}")

def send_admin_new_verification_request_email(admin_recipient_list: List[str], verification_request: VerificationRequest):
    """Notifies designated admins about a new verification request."""
    if not admin_recipient_list:
        logger.warning("No admin recipients provided for new verification request notification.")
        return
    if not verification_request:
        logger.warning("Attempted to send new verification request email with invalid request object.")
        return

    try:
        user = verification_request.user
        business = verification_request.business
        business_name = business.businessName if business else "N/A"
        user_email = user.email if user else "N/A"
    except AttributeError:
        logger.error(f"Could not access related data for verification request {verification_request.id} when sending admin notification.")
        return

    logger.info(f"Preparing new verification request notification email for request {verification_request.id} to admins: {admin_recipient_list}")

    # Construct URL to the admin verification section (adjust path as needed)
    verification_url = f"{settings.FRONTEND_BASE_URL}/admin/verification/{verification_request.id}" # Example URL

    context = {
        'verification_request': verification_request,
        'user': user,
        'business': business,
        'verification_url': verification_url,
        'recipient_email': ", ".join(admin_recipient_list), # For footer context, list all admins
    }
    send_templated_email(
        recipient_list=admin_recipient_list,
        template_name='emails/admin_new_verification_request.html',
        context=context,
        subject=f"New Verification Request Submitted: {business_name} ({user_email})"
    )
    logger.info(f"New verification request email prepared/queued for request {verification_request.id}")
    
def send_review_submission_confirmation_email(user: CustomUser, review: Reviews):
    """Sends confirmation after a user submits a review."""
    if not user or not user.email or not review:
        logger.warning("Attempted to send review confirmation with invalid user or review.")
        return

    class_name = "N/A" # Default value
    try:
        # Attempt to access the related class title
        if review.classId and review.classId.title:
            class_name = review.classId.title
            logger.debug(f"Successfully fetched class_name '{class_name}' from review.classId.title for review {review.reviewId}")
        else:
             logger.warning(f"review.classId or review.classId.title is missing for review {review.reviewId}. Using default '{class_name}'.")

    except AttributeError as e:
        logger.error(f"AttributeError getting class_name for review {review.reviewId}: {e}", exc_info=True)
        # class_name remains "N/A"

    logger.info(f"Preparing review submission confirmation email for review {review.reviewId} to user {user.email} with class_name='{class_name}'")

    logger.debug(f"Email Context Check (Review Confirm) for Review {review.reviewId}: User={user.email}, ClassNameContextValue='{class_name}'")

    context = {
        'user': user,
        'review': review,
        'class_name': class_name, # Pass the determined class_name
        'recipient_email': user.email, # Explicitly set
        'settings': settings, # Ensure settings are available if needed by base
    }

    # Use the determined class_name in the subject as well
    subject_class_name = class_name if class_name != "N/A" else "[Class Name]"
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/review_submission_confirmation_user.html',
        context=context,
        subject=f"We Received Your Review for {subject_class_name}"
    )
    logger.info(f"Review submission confirmation prepared/queued for review {review.reviewId}")


def send_review_response_notification_email(user: CustomUser, review: Reviews):
    """Notifies a user when a business responds to their review."""
    if not user or not user.email or not review:
        logger.warning("Attempted to send review response notification with invalid user or review.")
        return

    business_name = "N/A"
    class_id = None
    class_name = "N/A"
    try:
        business_name = review.businessId.businessName
        class_id = review.classId.classId
        class_name = review.classId.title
    except AttributeError:
        logger.error(f"Could not access related data for review {review.reviewId} when sending response notification.")
        # Allow sending even if data is missing, but log it.

    logger.info(f"Preparing review response notification email for review {review.reviewId} to user {user.email}")

    # Construct URL to the class page (or potentially a specific review anchor)
    review_url = "#" # Default if no class_id
    if class_id:
        review_url = f"{settings.FRONTEND_BASE_URL}/classes/{class_id}?review={review.reviewId}" # Example URL structure

    context = {
        'user': user,
        'review': review,
        'business_name': business_name, # Pass fetched name
        'class_name': class_name,     # Pass fetched class name
        'review_url': review_url,
        'recipient_email': user.email, # Explicitly set
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/review_response_notification_user.html',
        context=context,
        subject=f"{business_name} Responded to Your Review" # Use fetched name
    )
    logger.info(f"Review response notification prepared/queued for review {review.reviewId}")


# --- Support Ticket Related Email Trigger Functions ---

def send_support_ticket_created_email(user: CustomUser, ticket: SupportTicket):
    """Sends confirmation after a user creates a support ticket."""
    if not user or not user.email or not ticket:
        logger.warning("Attempted to send ticket created confirmation with invalid user or ticket.")
        return

    logger.info(f"Preparing support ticket created confirmation email for ticket {ticket.ticket_id} to user {user.email}")

    # Construct URL to the user's view of the ticket
    ticket_url = f"{settings.FRONTEND_BASE_URL}/support/tickets/{ticket.ticket_id}" # Example URL structure

    context = {
        'user': user,
        'ticket': ticket,
        'ticket_url': ticket_url,
        'recipient_email': user.email, # Explicitly set
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/support_ticket_created_user.html',
        context=context,
        subject=f"Support Ticket Received [#{ticket.ticket_id}] - {ticket.subject}"
    )
    logger.info(f"Support ticket created confirmation prepared/queued for ticket {ticket.ticket_id}")


def send_agent_reply_email(user: CustomUser, ticket: SupportTicket, agent: Optional[CustomUser] = None):
    """Notifies a user when a support agent replies to their ticket."""
    if not user or not user.email or not ticket:
        logger.warning("Attempted to send agent reply notification with invalid user or ticket.")
        return

    agent_name = agent.get_full_name() if agent and agent.get_full_name() else "A support agent"

    logger.info(f"Preparing agent reply notification email for ticket {ticket.ticket_id} to user {user.email}")

    ticket_url = f"{settings.FRONTEND_BASE_URL}/support/tickets/{ticket.ticket_id}" # Example URL structure

    context = {
        'user': user,
        'ticket': ticket,
        'agent_name': agent_name,
        'ticket_url': ticket_url,
        'recipient_email': user.email, # Explicitly set
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/support_agent_reply_user.html',
        context=context,
        subject=f"Re: Support Ticket [#{ticket.ticket_id}] - {ticket.subject}"
    )
    logger.info(f"Agent reply notification prepared/queued for ticket {ticket.ticket_id}")


def send_ticket_resolved_email(user: CustomUser, ticket: SupportTicket):
    """Notifies a user when their support ticket is resolved."""
    if not user or not user.email or not ticket:
        logger.warning("Attempted to send ticket resolved notification with invalid user or ticket.")
        return

    logger.info(f"Preparing ticket resolved notification email for ticket {ticket.ticket_id} to user {user.email}")

    ticket_url = f"{settings.FRONTEND_BASE_URL}/support/tickets/{ticket.ticket_id}" # Example URL structure

    context = {
        'user': user,
        'ticket': ticket,
        'ticket_url': ticket_url,
        'recipient_email': user.email, # Explicitly set
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/support_ticket_resolved_user.html',
        context=context,
        subject=f"Support Ticket Resolved [#{ticket.ticket_id}] - {ticket.subject}"
    )
    logger.info(f"Ticket resolved notification prepared/queued for ticket {ticket.ticket_id}")

def send_business_verification_approved_email(user: CustomUser, business: BusinessInfo):
    """Sends notification when a business verification is approved."""
    if not user or not user.email or not business:
        logger.warning("Attempted to send verification approved email with invalid user or business.")
        return

    logger.info(f"Preparing verification approved email for business {business.businessId} to user {user.email}")

    # Construct URL to the business dashboard
    dashboard_url = f"{settings.FRONTEND_BASE_URL}/dashboard" # Adjust path as needed

    context = {
        'user': user,
        'business': business,
        'dashboard_url': dashboard_url,
        'recipient_email': user.email, # Explicitly set
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/business_verification_approved.html',
        context=context,
        subject=f"Your Business '{business.businessName}' is Verified!"
    )
    logger.info(f"Verification approved email prepared/queued for business {business.businessId}")


def send_business_verification_rejected_email(user: CustomUser, business: BusinessInfo, verification_request: VerificationRequest):
    """Sends notification when a business verification is rejected."""
    if not user or not user.email or not business or not verification_request:
        logger.warning("Attempted to send verification rejected email with invalid user, business, or request.")
        return

    logger.info(f"Preparing verification rejected email for business {business.businessId} to user {user.email}")

    contact_email = settings.NOTIFICATION_SETTINGS.get('reply_to', 'support@classeasily.com')
    # settings_url = f"{settings.FRONTEND_BASE_URL}/dashboard/settings" # Example URL

    context = {
        'user': user,
        'business': business,
        'verification_request': verification_request,
        'contact_email': contact_email,
        # 'settings_url': settings_url,
        'recipient_email': user.email, # Explicitly set
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/business_verification_rejected.html',
        context=context,
        subject=f"Action Required: Update for Your Business Verification"
    )
    logger.info(f"Verification rejected email prepared/queued for business {business.businessId}")


def send_business_new_booking_email(business_user: CustomUser, booking: Booking):
    """Notifies a business user about a new booking."""
    if not business_user or not business_user.email or not booking:
        logger.warning("Attempted to send new booking email with invalid recipient or booking.")
        return

    # Use helper to get related data safely
    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_title'):
        logger.error(f"Could not access related class data for booking {booking.id} when sending new booking email.")
        return

    logger.info(f"Preparing new booking notification email for booking {booking.id} to business user {business_user.email}")

    # Construct URL to the specific booking on the business dashboard
    dashboard_booking_url = f"{settings.FRONTEND_BASE_URL}/dashboard/bookings/{booking.id}" # Example URL structure

    context = {
        'business_user': business_user,
        'booking': booking,
        'dashboard_booking_url': dashboard_booking_url,
        'recipient_email': business_user.email, # Explicitly set
        'related_data': related_data, # Pass fetched data
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name='emails/business_new_booking.html',
        context=context,
        subject=f"New Booking Received for {related_data['class_title']}" # Use fetched title
    )
    logger.info(f"New booking notification email prepared/queued for booking {booking.id}")


def send_business_student_cancellation_email(business_user: CustomUser, booking: Booking):
    """Notifies a business user when a student cancels a booking."""
    if not business_user or not business_user.email or not booking:
        logger.warning("Attempted to send student cancellation email with invalid recipient or booking.")
        return

    # Use helper to get related data safely
    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_title'):
        logger.error(f"Could not access related class data for booking {booking.id} when sending student cancellation email.")
        return

    logger.info(f"Preparing student cancellation notification email for booking {booking.id} to business user {business_user.email}")

    # Construct URL to the bookings list on the business dashboard
    dashboard_booking_url = f"{settings.FRONTEND_BASE_URL}/dashboard/bookings" # Example URL structure

    context = {
        'business_user': business_user,
        'booking': booking,
        'dashboard_booking_url': dashboard_booking_url,
        'recipient_email': business_user.email, # Explicitly set
        'related_data': related_data, # Pass fetched data
    }
    send_templated_email(
        recipient_list=[business_user.email],
        template_name='emails/business_booking_cancelled_by_student.html',
        context=context,
        subject=f"Booking Cancelled by Student for {related_data['class_title']}" # Use fetched title
    )
    logger.info(f"Student cancellation notification email prepared/queued for booking {booking.id}")