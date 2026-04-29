# Parent FK on ClassCollection + AI description fields on ClassesMain

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0233_corporate_booking_flow"),
    ]

    operations = [
        migrations.AddField(
            model_name="classcollection",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                help_text="Parent collection (top-level if null). Sub-collections are one level deep only.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="children",
                to="quickstart.classcollection",
                db_index=True,
            ),
        ),
        migrations.AddIndex(
            model_name="classcollection",
            index=models.Index(
                fields=["parent", "sort_order"], name="class_collect_parent_sort_idx"
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="description_summary",
            field=models.CharField(blank=True, default="", max_length=240),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="description_sections",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="description_ai_source_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="description_ai_status",
            field=models.CharField(
                choices=[
                    ("pending", "pending"),
                    ("ready", "ready"),
                    ("failed", "failed"),
                    ("stale", "stale"),
                ],
                db_index=True,
                default="stale",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="description_ai_generated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
