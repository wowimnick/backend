# Widget email branding (Growth/Advanced) for confirmation/reminder emails

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0201_discount_apply_to_widget"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="widget_email_branding",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Optional branding for widget booking emails: logo_url, primary_color, footer_text, confirmation_message.",
            ),
        ),
    ]
