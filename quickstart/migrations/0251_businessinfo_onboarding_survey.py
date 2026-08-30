from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0250_saas_registration_optional_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="onboarding_survey",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Optional SaaS signup answers: industry, booking_system, attribution, estimated_monthly_volume.",
            ),
        ),
    ]
