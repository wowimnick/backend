# Add cooldown field for "connect Stripe to receive payout" reminder emails

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0204_businessaddonsubscription"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="last_payout_connect_reminder_sent",
            field=models.DateTimeField(
                blank=True,
                help_text="When we last emailed this business to connect Stripe for pending payouts (3-day cooldown).",
                null=True,
            ),
        ),
    ]
