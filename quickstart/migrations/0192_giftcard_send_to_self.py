# Generated manually for send_to_self on GiftCard

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0191_giftcard_design_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="giftcard",
            name="send_to_self",
            field=models.BooleanField(
                default=False,
                help_text="True when purchaser chose 'Email to me' (recipient is themselves).",
            ),
        ),
    ]
