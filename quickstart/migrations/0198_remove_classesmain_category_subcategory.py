# Remove category and subcategory from ClassesMain. Platform uses collections only.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0197_classesmain_category_subcategory_nullable"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="classesmain",
            name="category",
        ),
        migrations.RemoveField(
            model_name="classesmain",
            name="subcategory",
        ),
    ]
