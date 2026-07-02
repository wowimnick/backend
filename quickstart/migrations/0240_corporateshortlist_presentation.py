# Add presentation JSONField to CorporateShortlist (parallel branch from 0239).
# Superseded for new deploys by 0249; kept so environments that already applied
# this migration name do not hit a missing-migration error.

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
