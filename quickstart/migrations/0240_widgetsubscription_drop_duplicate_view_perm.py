# Remove Meta.permissions entry that duplicated Django's built-in view_widgetsubscription (auth.E005).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0239_businessrole_membership_perms"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="widgetsubscription",
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
