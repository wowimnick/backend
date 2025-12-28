import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from django.test import override_settings

# Import your models and utils
from quickstart.models import (
    CustomUser, BusinessInfo, ClassesMain, ClassCategory, 
    ClassOption, Schedule, ScheduleInstance, Booking, 
    Reviews, SupportTicket, VerificationRequest, Payout, 
    CourseEnrollment, BusinessStaff, PermissionGroup, Role,
    BusinessRole  # <--- Added this import
)
from quickstart.utils import email_utils

class Command(BaseCommand):
    help = 'Tests every email template by sending samples to a specific address'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email', 
            type=str, 
            default='npopelnukh@gmail.com',
            help='The target email address for the tests'
        )

    # Force Celery to run synchronously so emails actually send during this script execution
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def handle(self, *args, **options):
        target_email = options['email']
        self.stdout.write(self.style.WARNING(f'Starting email test suite. Target: {target_email}'))
        self.stdout.write(self.style.WARNING('Note: This uses in-memory objects. No database records are created.'))

        # --- 1. SETUP MOCK DATA OBJECTS ---
        
        # User
        user = CustomUser(
            userId=1,
            email=target_email,
            first_name="Nikita",
            last_name="Popelnukh",
            username="nikita_test"
        )

        # Business
        business = BusinessInfo(
            businessId=1,
            businessName="Demo Yoga Studio",
            studentContactEmail="contact@demoyoga.com",
            businessCity="Toronto",
            business_timezone="America/Toronto",
            slug="demo-yoga-studio"
        )
        business.owner = user 

        # Class
        category = ClassCategory(name="Fitness")
        class_main = ClassesMain(
            classId=101,
            title="Advanced Vinyasa Flow",
            description="A challenging flow for experienced students.",
            location="123 Main St, Studio B",
            slug="advanced-vinyasa-flow",
            businessId=business,
            category=category
        )

        # Option
        option_single = ClassOption(
            optionId=50,
            classId=class_main,
            booking_type="Single Session",
            level="intermediate"
        )
        
        option_course = ClassOption(
            optionId=51,
            classId=class_main,
            booking_type="Full Course",
            level="all"
        )

        # Schedule
        schedule = Schedule(
            id=200,
            name="Monday Mornings",
            option=option_single,
            day="Mon",
            time=timezone.now().time(),
            duration=60,
            price=Decimal("25.00"),
            maxParticipants=20,
            minParticipants=1
        )

        # Instance (Future)
        instance_future = ScheduleInstance(
            id=300,
            schedule=schedule,
            date=timezone.now().date() + timedelta(days=2),
            time=timezone.now().time(),
            duration=60,
            price=Decimal("25.00"),
            max_participants=20
        )

        # Instance (Past/Old)
        instance_past = ScheduleInstance(
            id=299,
            schedule=schedule,
            date=timezone.now().date() - timedelta(days=2),
            time=timezone.now().time(),
            duration=60,
            price=Decimal("25.00"),
            max_participants=20
        )

        # Booking (Single Session)
        booking_single = Booking(
            id=5000,
            user=user,
            schedule_instance=instance_future,
            status="confirmed",
            booking_date=timezone.now(),
            participants=1,
            amount_paid=Decimal("25.00"),
            user_facing_reference="BKG-TEST-001",
            enrollment_type="Single Session"
        )
        
        # Booking (Course)
        booking_course = Booking(
            id=5001,
            user=user,
            schedule_instance=instance_future,
            status="confirmed",
            booking_date=timezone.now(),
            participants=1,
            amount_paid=Decimal("200.00"),
            user_facing_reference="BKG-COURSE-001",
            enrollment_type="Full Course",
            booking_group_id=uuid.uuid4(),
            course_session_number=1
        )

        # Enrollment
        enrollment = CourseEnrollment(
             id=uuid.uuid4(),
             schedule=schedule,
             user=user,
             booking_group_id=booking_course.booking_group_id,
             status="active",
             total_sessions=8,
             sessions_completed=1,
             participants=1,
             total_amount_paid=Decimal("200.00")
        )

        # Review
        review = Reviews(
            reviewId=1,
            userId=user,
            classId=class_main,
            businessId=business,
            rating=5,
            comment="Amazing class, loved the energy!",
            createdAt=timezone.now()
        )

        # Ticket
        ticket = SupportTicket(
            ticket_id=900,
            user_facing_id="SPT-900",
            user=user,
            subject="Trouble with payment",
            description="I cannot update my card.",
            status="open"
        )

        # Payout
        payout = Payout(
            id=uuid.uuid4(),
            business=business,
            amount=Decimal("1500.50"),
            currency="CAD",
            arrival_date=timezone.now().date() + timedelta(days=2),
            status="paid"
        )
        
        # Verification Request
        verif_req = VerificationRequest(
            id=uuid.uuid4(),
            user=user,
            business=business,
            status="pending",
            submitted_at=timezone.now()
        )

        # Staff Invitation
        # FIX: Instantiate an actual BusinessRole model instead of a namedtuple
        business_role = BusinessRole(
            id=uuid.uuid4(),
            business=business,
            name="Senior Instructor",
            description="Senior staff member"
        )
        
        invitation = BusinessStaff(
            id=uuid.uuid4(),
            business=business,
            role=business_role,  # <--- Assign actual model instance
            invited_email=target_email,
            invited_by=user,
            invitation_token=uuid.uuid4()
        )


        # --- 2. EXECUTE TESTS ---

        tests = [
            ("Welcome Email", lambda: email_utils.send_welcome_email(user)),
            
            ("Booking Confirmation (Single)", lambda: email_utils.send_booking_confirmation_email(user, booking_single)),
            
            ("Booking Confirmation (Course)", lambda: email_utils.send_booking_confirmation_email(user, booking_course)),
            
            ("Business Staff Invitation", lambda: email_utils.send_business_staff_invitation_email(invitation)),
            
            ("Account Security (Password Change)", lambda: email_utils.send_account_security_email(user, "password_change", subject="Security Alert")),
            
            ("Booking Cancellation (User Action)", lambda: email_utils.send_booking_cancellation_user_email(user, booking_single, "Full Refund to Original Payment Method")),
            
            ("Booking Cancelled by Business", lambda: email_utils.send_booking_cancelled_by_other_email(
                user, booking_single, "Instructor", "Instructor illness", "support@demoyoga.com"
            )),
            
            ("Booking Reminder", lambda: email_utils.send_booking_reminder_email(user, booking_single)),
            
            ("Admin: New Verification Request", lambda: email_utils.send_admin_new_verification_request_email([target_email], verif_req)),
            
            ("Review Submission Confirmation", lambda: email_utils.send_review_submission_confirmation_email(user, review)),
            
            ("Review Response Notification", lambda: email_utils.send_review_response_notification_email(user, review)),
            
            ("Support Ticket Created", lambda: email_utils.send_support_ticket_created_email(user, ticket)),
            
            ("Support Agent Reply", lambda: email_utils.send_agent_reply_email(user, ticket, agent=user)),
            
            ("Support Ticket Resolved", lambda: email_utils.send_ticket_resolved_email(user, ticket)),
            
            ("Business Verification Approved", lambda: email_utils.send_business_verification_approved_email(user, business)),
            
            ("Business Verification Rejected", lambda: email_utils.send_business_verification_rejected_email(user, business, verif_req)),
            
            ("Business: New Booking", lambda: email_utils.send_business_new_booking_email(user, booking_single)),
            
            ("Business: Student Cancellation", lambda: email_utils.send_business_student_cancellation_email(user, booking_single)),
            
            ("Business: Payout Initiated", lambda: email_utils.send_payout_initiated_email(user, payout)),
            
            ("Concierge Handover", lambda: email_utils.send_concierge_handover_email(user, "https://classeasily.com/claim/123")),
            
            ("Request for Review", lambda: email_utils.send_request_for_review_email(user, booking_single)),
            
            ("Favorited Class New Dates", lambda: email_utils.send_favorited_class_new_dates_email(user, class_main, schedule)),
            
            ("Admin: User Reply to Ticket", lambda: email_utils.send_admin_user_reply_notification([target_email], ticket)),
            
            ("Booking Rescheduled", lambda: email_utils.send_booking_rescheduled_by_business_email(user, booking_single, instance_past, instance_future)),
            
            ("Performance Summary", lambda: email_utils.send_performance_summary_email(user, {
                "total_revenue": 1250.00,
                "total_bookings": 45,
                "new_students": 12,
                "profile_views": 300
            }, "Weekly")),
        ]

        # Loop through and run
        self.stdout.write("\n" + "="*50)
        success_count = 0
        
        for name, func in tests:
            self.stdout.write(f"Testing: {name}...", ending='')
            try:
                # Execute the function
                func()
                self.stdout.write(self.style.SUCCESS(" SENT ✅"))
                success_count += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(" FAILED ❌"))
                self.stdout.write(self.style.ERROR(f"  Error: {str(e)}"))
                # Uncomment next line to see full trace if needed
                import traceback; traceback.print_exc()

        self.stdout.write("="*50)
        self.stdout.write(self.style.SUCCESS(f"\nDone. {success_count}/{len(tests)} emails processed."))
        self.stdout.write(f"Please check inbox: {target_email}")