# Align CorporateInquiry.company_size choices with attendee-focused buckets

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0234_classcollection_parent_and_description_ai"),
    ]

    operations = [
        migrations.AlterField(
            model_name="corporateinquiry",
            name="company_size",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Prefer not to say"),
                    ("1-10", "1–10"),
                    ("11-25", "11–25"),
                    ("26-50", "26–50"),
                    ("51-100", "51–100"),
                    ("101-250", "101–250"),
                    ("250+", "250+"),
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
