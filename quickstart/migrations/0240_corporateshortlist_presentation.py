# Add presentation JSONField to CorporateShortlist

import quickstart.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0239_businessrole_membership_perms"),
    ]

    operations = [
        migrations.AddField(
            model_name="corporateshortlist",
            name="presentation",
            field=models.JSONField(
                blank=True,
                default=quickstart.models.default_corporate_shortlist_presentation,
                help_text="Visual/copy settings for the public shortlist page",
            ),
        ),
    ]
