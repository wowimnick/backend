from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0220_business_marketing_footer_alignment"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="require_participant_names",
            field=models.BooleanField(
                default=False,
                help_text="When True, checkout must collect a distinct name for each participant when booking more than one spot.",
            ),
        ),
    ]
