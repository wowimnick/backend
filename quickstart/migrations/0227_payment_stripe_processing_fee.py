# Payment.stripe_processing_fee — disclosed processing cost deducted from business payout

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0226_widget_funnel_event"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="stripe_processing_fee",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Estimated Stripe card processing fee (2.9 percent + fixed) deducted from the business payout, not from platform commission.",
                max_digits=10,
            ),
        ),
    ]
