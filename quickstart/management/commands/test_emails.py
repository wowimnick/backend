# quickstart/tests/management/commands/test_emails.py

import logging
import uuid
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import transaction

# Import your factory-boy factories
from quickstart.models import BusinessInfo, CustomUser
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    BookingFactory,
    ReviewFactory,
    SupportTicketFactory,
    VerificationRequestFactory,
    PayoutFactory,
)

# Import all email sending functions
from quickstart.utils.email_utils import (
    send_welcome_email,
    send_account_security_email,
    send_booking_confirmation_email,
    send_booking_cancellation_user_email,
    send_booking_cancelled_by_other_email,
    send_booking_reminder_email,
    send_review_submission_confirmation_email,
    send_review_response_notification_email,
    send_support_ticket_created_email,
    send_agent_reply_email,
    send_ticket_resolved_email,
    send_business_verification_approved_email,
    send_business_verification_rejected_email,
    send_admin_new_verification_request_email,
    send_business_new_booking_email,
    send_business_student_cancellation_email,
    send_payout_initiated_email,
    send_performance_summary_email,
    send_class_nearing_full_email,
    send_request_for_review_email,
    send_favorited_class_new_dates_email,
    send_admin_user_reply_notification,
)

# Configure logger for the command
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sends one or all test emails to a specified address using factory-generated data."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created_objects = {
            "users": [],
            "businesses": [],
            "bookings": [],
            "reviews": [],
            "tickets": [],
            "verification_requests": [],
            "class_categories": [],
            "class_subcategories": [],
            "classes": [],
            "class_options": [],
            "schedules": [],
            "schedule_instances": [],
            "payouts": [],
        }

    def add_arguments(self, parser):
        """Adds command-line arguments for email and test name."""
        parser.add_argument(
            "email",
            type=str,
            help="The recipient email address for all test emails (user, business, admin).",
        )
        parser.add_argument(
            "--test",
            type=str,
            nargs="?",
            default=None,
            help="Optional: The specific name of the email test to run (e.g., 'booking_confirmation').",
        )
        parser.add_argument(
            "--keep-data",
            action="store_true",
            help="Keep the test data after running (don't clean up).",
        )

    def _generate_unique_stripe_id(self):
        """Generate a unique Stripe account ID for testing."""
        return f"acct_test_{uuid.uuid4().hex[:8]}"

    def _setup_mock_data(self, recipient_email, test_name_filter=None):
        """
        Creates mock objects for generating emails. Data is created conditionally
        based on the specific test being run to ensure correct preconditions.
        """
        self.stdout.write("Setting up mock data for email tests using factories...")
        unique_suffix = uuid.uuid4().hex[:8]

        # --- Unified User Setup ---
        try:
            user = CustomUser.objects.get(email=recipient_email)
            self.stdout.write(f"Using existing user for all roles: {user.email}")
        except CustomUser.DoesNotExist:
            user_username = f"test_user_{unique_suffix}"
            user = CustomUser.objects.create(
                username=user_username,
                email=recipient_email,
                first_name="Test",
                last_name="User",
            )
            self.created_objects["users"].append(user.pk)
            self.stdout.write(f"Created new test user for all roles: {user.email}")

        # --- Business Setup ---
        try:
            business = BusinessInfo.objects.get(owner=user)
            self.stdout.write(
                f"Found existing business '{business.businessName}' for {user.email}."
            )
        except BusinessInfo.DoesNotExist:
            self.stdout.write(
                f"No existing business found for {user.email}, creating one..."
            )
            business = BusinessInfoFactory(
                owner=user,
                studentContactEmail=user.email,
                stripe_account_id=self._generate_unique_stripe_id(),
            )
            self.created_objects["businesses"].append(business.pk)

        booking = BookingFactory(
            user=user,
            schedule_instance__schedule__option__classId__businessId=business,
            status="confirmed",
            payment_status="paid",
        )
        self.created_objects["bookings"].append(booking.pk)
        self.created_objects["schedule_instances"].append(booking.schedule_instance.pk)
        self.created_objects["schedules"].append(booking.schedule_instance.schedule.pk)
        self.created_objects["class_options"].append(
            booking.schedule_instance.schedule.option.pk
        )
        self.created_objects["classes"].append(
            booking.schedule_instance.schedule.option.classId.pk
        )

        class_obj = booking.schedule_instance.schedule.option.classId
        if class_obj.category.pk not in self.created_objects["class_categories"]:
            self.created_objects["class_categories"].append(class_obj.category.pk)
        if class_obj.subcategory.pk not in self.created_objects["class_subcategories"]:
            self.created_objects["class_subcategories"].append(class_obj.subcategory.pk)
        class_obj.favorited_by.add(user)

        # --- Conditional Data Creation ---
        review = None
        # A review should only be created for tests that need it.
        tests_requiring_review = ["review_submission", "review_response"]
        if test_name_filter is None or test_name_filter in tests_requiring_review:
            review = ReviewFactory(
                userId=user,
                classId=class_obj,
                booking=booking,
                business_response="We are glad you enjoyed the test!",
            )
            self.created_objects["reviews"].append(review.pk)

        ticket = SupportTicketFactory(user=user)
        self.created_objects["tickets"].append(ticket.pk)

        verification_request = VerificationRequestFactory(
            user=user,
            business=business,
            rejection_reason="Test rejection reason: missing documentation.",
        )
        self.created_objects["verification_requests"].append(verification_request.pk)

        payout = None
        # Only create a Payout if running all tests or the specific payout test.
        if test_name_filter is None or test_name_filter == "payout_initiated":
            payout = PayoutFactory(business=business)
            self.created_objects["payouts"].append(payout.pk)

        self.stdout.write(self.style.SUCCESS("Mock data setup complete."))
        return {
            "user": user,
            "business_owner": user,
            "business": business,
            "booking": booking,
            "review": review,
            "ticket": ticket,
            "verification_request": verification_request,
            "payout": payout,
            "class_main": class_obj,
            "schedule": booking.schedule_instance.schedule,
            "schedule_instance": booking.schedule_instance,
        }

    def _cleanup_test_data(self):
        """Clean up all test data created during the command execution."""
        self.stdout.write("Cleaning up test data...")

        from quickstart.models import (
            Reviews,
            SupportTicket,
            VerificationRequest,
            Booking,
            ScheduleInstance,
            Schedule,
            ClassOption,
            ClassesMain,
            ClassSubcategory,
            ClassCategory,
            BusinessInfo,
            CustomUser,
            Payout,
        )

        cleanup_models = [
            (Payout, "payouts"),
            (Reviews, "reviews"),
            (SupportTicket, "tickets"),
            (VerificationRequest, "verification_requests"),
            (Booking, "bookings"),
            (ScheduleInstance, "schedule_instances"),
            (Schedule, "schedules"),
            (ClassOption, "class_options"),
            (ClassesMain, "classes"),
            (ClassSubcategory, "class_subcategories"),
            (ClassCategory, "class_categories"),
            (BusinessInfo, "businesses"),
            (CustomUser, "users"),
        ]

        with transaction.atomic():
            for model, object_type in cleanup_models:
                if self.created_objects[object_type]:
                    try:
                        if model == CustomUser:
                            users_to_clear = model.objects.filter(
                                pk__in=self.created_objects[object_type]
                            )
                            for user in users_to_clear:
                                if hasattr(user, "favorited_classes"):
                                    user.favorited_classes.clear()

                        deleted_count = model.objects.filter(
                            pk__in=self.created_objects[object_type]
                        ).delete()[0]

                        if deleted_count > 0:
                            self.stdout.write(f"Deleted {deleted_count} {object_type}")
                    except Exception as e:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Could not delete some {object_type}: {e}"
                            )
                        )

        self.stdout.write(self.style.SUCCESS("Test data cleanup complete."))

    def handle(self, *args, **options):
        """Main handler for the command."""
        recipient_email = options["email"]
        test_name_filter = options["test"]
        keep_data = options["keep_data"]

        self.stdout.write(
            f"Starting email test process for recipient: {recipient_email}"
        )

        original_celery_eager = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
        settings.CELERY_TASK_ALWAYS_EAGER = True
        self.stdout.write(
            self.style.WARNING(
                "Temporarily running Celery tasks synchronously for this test."
            )
        )

        try:
            # Use a transaction to ensure data is rolled back on failure
            with transaction.atomic():
                mock_data = self._setup_mock_data(recipient_email, test_name_filter)
                user = mock_data["user"]
                booking = mock_data["booking"]
                test_recipient_user = user

                performance_summary_data = {
                    "total_revenue": "1,234.56",
                    "new_bookings": 15,
                    "unique_students": 12,
                    "start_date": "Jan 01",
                    "end_date": "Jan 07, 2024",
                }

                all_tests = {
                    # User-facing emails
                    "welcome": lambda: send_welcome_email(test_recipient_user),
                    "account_security_password": lambda: send_account_security_email(
                        test_recipient_user,
                        "password",
                        subject="Test: Your Password Was Changed",
                    ),
                    "account_security_email": lambda: send_account_security_email(
                        test_recipient_user,
                        "email_update",
                        new_email=test_recipient_user.email,
                        subject="Test: Your Email Was Updated",
                    ),
                    "booking_confirmation": lambda: send_booking_confirmation_email(
                        test_recipient_user, booking
                    ),
                    "booking_cancellation_user": lambda: send_booking_cancellation_user_email(
                        test_recipient_user,
                        booking,
                        "A test refund of $99.98 has been issued.",
                    ),
                    "booking_cancelled_by_other": lambda: send_booking_cancelled_by_other_email(
                        user=test_recipient_user,
                        booking=booking,
                        cancelled_by="the business",
                        reason="This is a test cancellation by the business.",
                        contact_info="the business directly",
                    ),
                    "booking_reminder": lambda: send_booking_reminder_email(
                        test_recipient_user, booking
                    ),
                    "review_submission": lambda: send_review_submission_confirmation_email(
                        test_recipient_user, mock_data["review"]
                    ),
                    "review_response": lambda: send_review_response_notification_email(
                        test_recipient_user, mock_data["review"]
                    ),
                    "request_for_review": lambda: send_request_for_review_email(
                        test_recipient_user, booking
                    ),
                    "favorited_class_new_dates": lambda: send_favorited_class_new_dates_email(
                        test_recipient_user,
                        mock_data["class_main"],
                        mock_data["schedule"],
                    ),
                    # Support emails
                    "support_ticket_created": lambda: send_support_ticket_created_email(
                        test_recipient_user, mock_data["ticket"]
                    ),
                    "support_agent_reply": lambda: send_agent_reply_email(
                        test_recipient_user, mock_data["ticket"], test_recipient_user
                    ),
                    "support_ticket_resolved": lambda: send_ticket_resolved_email(
                        test_recipient_user, mock_data["ticket"]
                    ),
                    # Business-facing emails
                    "verification_approved": lambda: send_business_verification_approved_email(
                        test_recipient_user, mock_data["business"]
                    ),
                    "verification_rejected": lambda: send_business_verification_rejected_email(
                        test_recipient_user,
                        mock_data["business"],
                        mock_data["verification_request"],
                    ),
                    "business_new_booking": lambda: send_business_new_booking_email(
                        test_recipient_user, booking
                    ),
                    "business_student_cancellation": lambda: send_business_student_cancellation_email(
                        test_recipient_user, booking
                    ),
                    "payout_initiated": lambda: send_payout_initiated_email(
                        test_recipient_user, mock_data["payout"]
                    ),
                    "performance_summary": lambda: send_performance_summary_email(
                        test_recipient_user, performance_summary_data, "Weekly"
                    ),
                    "class_nearing_full": lambda: send_class_nearing_full_email(
                        test_recipient_user,
                        mock_data["schedule_instance"],
                        occupancy_percentage=85.0,
                    ),
                    # Admin-facing emails
                    "admin_new_verification": lambda: send_admin_new_verification_request_email(
                        [recipient_email], mock_data["verification_request"]
                    ),
                    "admin_user_reply_notification": lambda: send_admin_user_reply_notification(
                        [recipient_email], mock_data["ticket"]
                    ),
                }

                # Determine which tests to run
                tests_to_run = {}
                if test_name_filter:
                    if test_name_filter in all_tests:
                        # --- FIX: Specific data validation ---
                        tests_requiring_review = [
                            "review_submission",
                            "review_response",
                        ]
                        if (
                            test_name_filter in tests_requiring_review
                            and not mock_data.get("review")
                        ):
                            raise CommandError(
                                f"Test '{test_name_filter}' requires a review object, but it was not created. This is a setup logic error."
                            )

                        if (
                            test_name_filter == "payout_initiated"
                            and not mock_data.get("payout")
                        ):
                            raise CommandError(
                                f"Test '{test_name_filter}' requires a payout object, but it was not created. This is a setup logic error."
                            )

                        tests_to_run = {test_name_filter: all_tests[test_name_filter]}
                    else:
                        available_tests = ", ".join(all_tests.keys())
                        raise CommandError(
                            f"Test '{test_name_filter}' not found. Available tests are: {available_tests}"
                        )
                else:
                    tests_to_run = all_tests

                # Execute the tests
                for name, test_func in tests_to_run.items():
                    self.stdout.write(f"--- Running test: {name} ---")
                    try:
                        test_func()
                        self.stdout.write(
                            self.style.SUCCESS(f"Successfully queued '{name}' email.")
                        )
                    except Exception as e:
                        self.stderr.write(
                            self.style.ERROR(f"Error while running test '{name}': {e}")
                        )
                        logger.exception(f"Failed to execute email test '{name}'")

                self.stdout.write(self.style.SUCCESS("\nEmail test command finished."))

                if not keep_data:
                    # Cleanup will happen automatically when the transaction commits
                    pass
                else:
                    # If we need to keep data, we must not be in a transaction or commit it.
                    # This implementation relies on the transaction rolling back on error
                    # and committing on success. For keeping data, we'll just inform the user.
                    self.stdout.write(
                        self.style.WARNING(
                            "Test data kept (--keep-data flag was used). Manual cleanup may be needed."
                        )
                    )

            # If we are keeping data, we need to call cleanup explicitly outside the transaction
            if not keep_data:
                self._cleanup_test_data()

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"An unexpected error occurred: {e}"))
            logger.exception("An unexpected error occurred in the test_emails command")
            # Transaction will automatically roll back, so no data will be left
            self.stdout.write(
                self.style.WARNING(
                    "Transaction rolled back due to error. No data was saved."
                )
            )

        finally:
            settings.CELERY_TASK_ALWAYS_EAGER = original_celery_eager
            self.stdout.write(
                self.style.WARNING("Restored original Celery task behavior.")
            )
