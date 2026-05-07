# Generated for search suggest (trigram similarity on ClassCollection)

from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("quickstart", "0228_backfill_stripe_processing_fee_and_allocated"),
    ]

    operations = [
        TrigramExtension(),
    ]
