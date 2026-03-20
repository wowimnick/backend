# Membership product settings: confirmation message, welcome URL, custom signup fields, caps, trial; custom_data on CustomerMembership

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0209_add_membership_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="membershipproduct",
            name="confirmation_message",
            field=models.TextField(
                blank=True,
                help_text="Shown to the member after they subscribe. Leave blank for default.",
            ),
        ),
        migrations.AddField(
            model_name="membershipproduct",
            name="welcome_url",
            field=models.CharField(
                blank=True,
                help_text="e.g. https://yoursite.com/members — shown as 'Access member portal' on success screen",
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="membershipproduct",
            name="application_instructions",
            field=models.TextField(
                blank=True,
                help_text="Shown above the signup form when requires_approval=True.",
            ),
        ),
        migrations.AddField(
            model_name="membershipproduct",
            name="signup_fields",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Array of { key, label, type, required, options? } for custom signup form fields",
            ),
        ),
        migrations.AddField(
            model_name="membershipproduct",
            name="max_members",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Cap on active members; null = unlimited",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="membershipproduct",
            name="trial_period_days",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Free trial days before first charge; passed to Stripe",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="customermembership",
            name="custom_data",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Submitted signup field values from widget (key: value)",
            ),
        ),
        migrations.AlterField(
            model_name="customermembership",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Active"),
                    ("trialing", "Trialing"),
                    ("past_due", "Past Due"),
                    ("canceled", "Canceled"),
                    ("paused", "Paused"),
                    ("pending_approval", "Pending Approval"),
                    ("approved_pending_payment", "Approved (pending payment)"),
                    ("incomplete", "Incomplete"),
                ],
                db_index=True,
                default="active",
                max_length=30,
            ),
        ),
    ]
