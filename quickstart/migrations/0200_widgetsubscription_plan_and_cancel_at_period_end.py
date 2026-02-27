# Add plan_id and cancel_at_period_end to WidgetSubscription for Basic/Growth/Advanced tiers

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0199_widget_subscription_and_business_stripe_customer"),
    ]

    operations = [
        migrations.AddField(
            model_name="widgetsubscription",
            name="plan_id",
            field=models.CharField(
                choices=[
                    ("basic", "Basic"),
                    ("growth", "Growth"),
                    ("advanced", "Advanced"),
                ],
                db_index=True,
                default="basic",
                help_text="Plan tier: basic, growth, advanced.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="widgetsubscription",
            name="cancel_at_period_end",
            field=models.BooleanField(
                default=False,
                help_text="If True, subscription will end at current_period_end.",
            ),
        ),
    ]
