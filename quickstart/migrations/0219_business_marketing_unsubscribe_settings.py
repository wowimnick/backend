# Generated manually for marketing footer customization

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0218_add_manage_email_marketing_permission"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessmarketingsettings",
            name="unsubscribe_text",
            field=models.CharField(
                default="Unsubscribe",
                help_text="Label for the marketing unsubscribe control in email footers.",
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="businessmarketingsettings",
            name="unsubscribe_style",
            field=models.CharField(
                default="link",
                help_text="link | button",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="businessmarketingsettings",
            name="unsubscribe_color",
            field=models.CharField(
                default="#6366f1",
                help_text="Hex color for unsubscribe link or button background.",
                max_length=16,
            ),
        ),
    ]
