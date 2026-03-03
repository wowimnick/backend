# Add apply_to_widget to Discount for widget checkout eligibility (Growth/Advanced)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0200_widgetsubscription_plan_and_cancel_at_period_end"),
    ]

    operations = [
        migrations.AddField(
            model_name="discount",
            name="apply_to_widget",
            field=models.BooleanField(
                default=False,
                help_text="When True, this discount can be applied at widget checkout as well as marketplace. Requires Growth or Advanced widget plan.",
            ),
        ),
    ]
