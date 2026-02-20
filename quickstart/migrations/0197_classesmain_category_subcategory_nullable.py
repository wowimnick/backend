# Generated migration: make category and subcategory nullable on ClassesMain.
# Platform is moving to collections-only; categories/subcategories are no longer required.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0196_add_conversation_read_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="classesmain",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="classes_in_category",
                to="quickstart.classcategory",
            ),
        ),
    ]
