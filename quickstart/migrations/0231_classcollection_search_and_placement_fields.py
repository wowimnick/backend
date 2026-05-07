# Generated manually for ClassCollection discovery & placement fields.

from django.contrib.postgres.indexes import GinIndex
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0230_processed_stripe_events_and_checkout_attempts"),
    ]

    operations = [
        migrations.AddField(
            model_name="classcollection",
            name="search_aliases",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Lowercased strings matched for keyword search, e.g. pottery, ceramics.",
            ),
        ),
        migrations.AddField(
            model_name="classcollection",
            name="is_searchable",
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text="Include in keyword/suggest matching.",
            ),
        ),
        migrations.AddField(
            model_name="classcollection",
            name="show_in_i_want",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Show in homepage 'I want...' picker and corporate category strip.",
            ),
        ),
        migrations.AddField(
            model_name="classcollection",
            name="show_in_featured_categories",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Show on homepage featured categories strip.",
            ),
        ),
        migrations.AddField(
            model_name="classcollection",
            name="show_on_homepage_rows",
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text="Include in homepage collection carousels/rows.",
            ),
        ),
        migrations.AddField(
            model_name="classcollection",
            name="icon_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Lucide icon name for chips (e.g. Palette).",
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="classcollection",
            name="color",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional hex color for UI chips.",
                max_length=20,
            ),
        ),
        migrations.AddIndex(
            model_name="classcollection",
            index=GinIndex(
                fields=["search_aliases"],
                name="class_collections_search_aliases_gin",
            ),
        ),
    ]
