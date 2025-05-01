import logging
from django.core.mail import EmailMultiAlternatives # Keep for type hints maybe
from django.template.loader import render_to_string
from django.conf import settings
from django.utils import timezone
from typing import List, Optional
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

# --- Helper function to safely get related data ---
def _get_booking_related_data(booking: Booking) -> dict:
    """Safely retrieves common related data for booking emails."""
    data = {
        'class_title': "N/A",
        'class_id': None,
        'business_name': "N/A",
        'option_title': "N/A",
        'class_location': "N/A",
    }
    try:
        schedule_instance = booking.schedule_instance
        schedule = schedule_instance.schedule
        option = schedule.option
        class_main = option.classId
        business = class_main.businessId

        data['class_title'] = class_main.title
        data['class_id'] = class_main.classId
        data['business_name'] = business.businessName
        data['option_title'] = option.title
        data['class_location'] = class_main.location

    except AttributeError as e:
        logger.error(f"Could not access related data for booking {booking.id} when preparing email context: {e}", exc_info=True)
    return data

# --- Main Function to Send Templated Emails (NOW USES TASK QUEUE) ---

def send_templated_email(recipient_list, template_name, context, subject=None):
    """
    Renders an email using a template and queues it for sending via Celery.

    Args:
        recipient_list (list): List of email addresses (strings).
        template_name (str): Path to the template file (e.g., 'emails/welcome_user.html').
        context (dict): Dictionary of context variables for the template.
        subject (str, optional): Email subject. If None, tries to derive from template title.
    """
    if not recipient_list:
        logger.warning("No recipients provided for template %s, skipping email queuing.", template_name)
        return

    # --- Context Preparation ---
    # Ensure recipient_email is always present for the footer
    context.setdefault('recipient_email', recipient_list[0] if recipient_list else "N/A")
    # Ensure base URL is always present
    context.setdefault('frontend_base_url', settings.FRONTEND_BASE_URL)
    # Add settings to context IF needed by templates directly (prefer filters though)
    context.setdefault('settings', settings) # Make settings available if direct access is needed

    # --- Render Email Content ---
    try:
        html_content = render_to_string(template_name, context)
    except Exception as e:
        logger.error(f"Error rendering email template '{template_name}' with context keys {list(context.keys())}: {e}", exc_info=True)
        return # Stop processing if template rendering fails

    # --- Determine Subject ---
    # Using explicit subject is preferred. This block remains as a fallback.
    if not subject:
        try:
            # Attempt to extract subject from the template's title block
            from django.template import Template, Context
            # Simple extraction assuming {% block title %}...{% endblock %}
            minimal_template_string = "{% extends '" + template_name + "' %}{% block title %}{% endblock %}"
            template_obj = Template(minimal_template_string)
            subject = template_obj.render(Context(context)).strip()
            # Provide a default if the block is empty or extraction fails
            if not subject:
                 subject = "Notification from ClassEasily"
                 logger.warning(f"Subject block empty or not found in '{template_name}'. Using default.")
        except Exception as e:
            logger.warning(f"Could not derive subject from template '{template_name}': {e}. Using default.")
            subject = "Notification from ClassEasily" # Fallback on any error
    subject = subject or "Notification from ClassEasily" # Final fallback

    # --- Prepare Arguments for Task ---
    try:
        # Prepare arguments for the Celery task
        task_kwargs = {
            'subject': subject,
            'body': "Please view this email in an HTML-compatible client.", # Plain text fallback
            'from_email': settings.DEFAULT_FROM_EMAIL,
            'to_list': recipient_list,
            'reply_to_list': [settings.NOTIFICATION_SETTINGS.get('reply_to')] if settings.NOTIFICATION_SETTINGS.get('reply_to') else None,
            'html_content': html_content
            # 'attachments': None # TODO: Add attachment handling if needed later
        }

        # --- Dispatch the Task ---
        send_email_task.delay(**task_kwargs) # Use .delay() to queue the task

        logger.info(f"Email task queued for {recipient_list} using template '{template_name}'. Subject: '{subject}'.")

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
    """
    Sends the booking confirmation email to a user after successful payment.

    Args:
        user (CustomUser): The user who made the booking.
        booking (Booking): The confirmed Booking object.
    """
    if not user or not user.email or not booking:
        logger.warning("Attempted to send booking confirmation with invalid user or booking.")
        return

    # Use helper to get related data safely
    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_id'): # Check if essential data was retrieved
        logger.error(f"Could not access essential related data for booking {booking.id} when sending confirmation.")
        return # Avoid sending incomplete emails

    logger.info(f"Preparing booking confirmation email for booking {booking.id} to user {user.email}")

    # Construct necessary URLs
    class_details_url = f"{settings.FRONTEND_BASE_URL}/classes/{related_data['class_id']}"
    manage_bookings_url = f"{settings.FRONTEND_BASE_URL}/my-classes"

    # ----> ADDED DEBUG LOGGING <----
    payment = booking.payments.filter(status='succeeded').first() # Attempt to get the payment
    logger.debug(f"Email Context Check for Booking {booking.id}: User={user.email}, ClassTitle='{related_data.get('class_title')}', BookingID={booking.id}, PaymentFound={'Yes' if payment else 'No'}, CardLast4='{payment.card_last4 if payment else 'N/A'}'")
    # ----> END DEBUG LOGGING <----

    context = {
        'user': user,
        'booking': booking, # Pass the full booking object
        'class_details_url': class_details_url,
        'manage_bookings_url': manage_bookings_url,
        'recipient_email': user.email, # Explicitly set for footer
        'related_data': related_data, # Pass fetched related data dictionary
        'payment': payment # Explicitly pass the first successful payment if found
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/booking_confirmation_user.html',
        context=context,
        subject=f"Your Booking for {related_data['class_title']} is Confirmed!" # Use fetched title
    )
    logger.info(f"Booking confirmation email prepared/queued for booking {booking.id}")


def send_booking_cancellation_user_email(user: CustomUser, booking: Booking, refund_details: str):
    """
    Sends confirmation to a user after they cancelled their booking.

    Args:
        user (CustomUser): The user who cancelled the booking.
        booking (Booking): The cancelled Booking object.
        refund_details (str): A message describing the refund status.
    """
    if not user or not user.email or not booking:
        logger.warning("Attempted to send user booking cancellation email with invalid user or booking.")
        return

    # Use helper to get related data safely
    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_title'):
        logger.error(f"Could not access related data for booking {booking.id} when sending user cancellation email.")
        # Don't stop, try to send with available info, but log error

    logger.info(f"Preparing user booking cancellation email for booking {booking.id} to user {user.email}")

    explore_url = f"{settings.FRONTEND_BASE_URL}/explore"

    schedule_instance = booking.schedule_instance # Get instance for logging
    instance_date = schedule_instance.date if schedule_instance else "N/A"
    instance_time = schedule_instance.time if schedule_instance else "N/A"
    class_title_from_data = related_data.get('class_title', '[TITLE MISSING]')
    logger.debug(f"Email Context Check (Cancellation) for Booking {booking.id}: User={user.email}, ClassTitle='{class_title_from_data}', InstanceDate={instance_date}, InstanceTime={instance_time}")

    context = {
        'user': user,
        'booking': booking, # Pass the full booking object
        'refund_details': refund_details,
        'explore_url': explore_url,
        'recipient_email': user.email, # Explicitly set
        'related_data': related_data, # Pass fetched data
        'schedule_instance': schedule_instance # Explicitly pass instance if needed
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/booking_cancellation_user.html',
        context=context,
        subject=f"Your Booking for {class_title_from_data} Has Been Cancelled" # Use fetched title (with fallback)
    )
    logger.info(f"User booking cancellation email prepared/queued for booking {booking.id}")


def send_booking_cancelled_by_other_email(user: CustomUser, booking: Booking, cancelled_by: str, reason: str, contact_info: str):
    """
    Sends notification to a user when their booking is cancelled by the business or an admin.

    Args:
        user (CustomUser): The user whose booking was cancelled.
        booking (Booking): The cancelled Booking object.
        cancelled_by (str): Description of who cancelled (e.g., "the business", "an administrator").
        reason (str): The reason for cancellation (can be empty).
        contact_info (str): Contact details for questions (business email/phone or support email).
    """
    if not user or not user.email or not booking:
        logger.warning("Attempted to send 'cancelled by other' email with invalid user or booking.")
        return

    # Use helper to get related data safely
    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_title'):
        logger.error(f"Could not access related data for booking {booking.id} when sending 'cancelled by other' email.")
        return

    logger.info(f"Preparing 'cancelled by other' email for booking {booking.id} to user {user.email}")

    explore_url = f"{settings.FRONTEND_BASE_URL}/explore"

    context = {
        'user': user,
        'booking': booking,
        'cancelled_by': cancelled_by,
        'reason': reason or "No specific reason provided.", # Provide a default if reason is empty
        'contact_info': contact_info,
        'explore_url': explore_url,
        'recipient_email': user.email, # Explicitly set
        'related_data': related_data, # Pass fetched data
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/booking_cancelled_by_other.html', # Check template name
        context=context,
        subject=f"Update: Your Booking for {related_data['class_title']} Was Cancelled" # Use fetched title
    )
    logger.info(f"'Cancelled by other' email prepared/queued for booking {booking.id}")


def send_booking_reminder_email(user: CustomUser, booking: Booking):
    """
    Sends a reminder email to a user about an upcoming class.

    Args:
        user (CustomUser): The user who has the booking.
        booking (Booking): The upcoming Booking object.
    """
    if not user or not user.email or not booking:
        logger.warning("Attempted to send booking reminder with invalid user or booking.")
        return

    # Use helper to get related data safely
    related_data = _get_booking_related_data(booking)
    if not related_data.get('class_id'):
        logger.error(f"Could not access related data for booking {booking.id} when sending reminder email.")
        return

    logger.info(f"Preparing booking reminder email for booking {booking.id} to user {user.email}")

    class_details_url = f"{settings.FRONTEND_BASE_URL}/classes/{related_data['class_id']}" # Construct URL

    context = {
        'user': user,
        'booking': booking,
        'class_details_url': class_details_url,
        'recipient_email': user.email, # Explicitly set
        'related_data': related_data, # Pass fetched data
    }
    send_templated_email(
        recipient_list=[user.email],
        template_name='emails/booking_reminder_user.html',
        context=context,
        subject=f"Reminder: Your Class '{related_data['class_title']}' is Soon!" # Use fetched title
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