# Enforce one WidgetSubscription per business (OneToOne)

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0207_collapse_widget_subscription_one_per_business"),
    ]

    operations = [
        migrations.AlterField(
            model_name="widgetsubscription",
            name="business",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="widget_subscription",
                to="quickstart.businessinfo",
            ),
        ),
    ]
