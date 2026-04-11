# Generated manually for new Notification.notification_type choices

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0222_searchlog"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("new_booking", "New Booking"),
                    ("booking_cancelled_by_user", "Booking Cancelled by User"),
                    ("booking_cancelled_by_biz", "Booking Cancelled by Business"),
                    ("class_reminder_biz", "Class Reminder for Business"),
                    ("class_reminder_student", "Class Reminder for Student"),
                    ("new_review", "New Review"),
                    ("review_response", "Review Response from Business"),
                    ("payment_succeeded", "Payment Succeeded"),
                    ("payment_failed", "Payment Failed"),
                    ("payout_initiated", "Payout Initiated"),
                    ("stripe_action_required", "Stripe Action Required"),
                    ("profile_incomplete", "Profile Incomplete"),
                    ("system_announcement", "System Announcement"),
                    ("new_message_support", "New Message in Support Ticket"),
                    ("new_message_chat", "New Message in Conversation"),
                    ("staff_joined", "Staff Member Joined"),
                    ("membership_new", "New Membership"),
                    ("membership_cancelled", "Membership Cancelled"),
                    ("membership_renewed", "Membership Renewed"),
                    ("booking_completed", "Booking Completed"),
                ],
                max_length=50,
            ),
        ),
    ]
