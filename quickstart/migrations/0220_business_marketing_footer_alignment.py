from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0219_business_marketing_unsubscribe_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessmarketingsettings",
            name="footer_alignment",
            field=models.CharField(
                default="left",
                help_text="left | center | right — text alignment for compliance footer block.",
                max_length=16,
            ),
        ),
    ]
