# Add read status fields to Conversation

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0195_guest_business_messaging"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="last_read_by_booker_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="conversation",
            name="last_read_by_business_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
