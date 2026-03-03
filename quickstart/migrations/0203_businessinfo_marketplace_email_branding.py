# Marketplace email branding addon

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0202_businessinfo_widget_email_branding"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="marketplace_email_branding_enabled",
            field=models.BooleanField(
                default=False,
                help_text="When True, marketplace booking emails use marketplace_email_branding (addon).",
            ),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="marketplace_email_branding",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Branding for marketplace booking emails when addon is enabled: logo_url, primary_color, footer_text, confirmation_message.",
            ),
        ),
    ]
