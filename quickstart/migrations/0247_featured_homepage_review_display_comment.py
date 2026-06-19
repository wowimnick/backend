from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0246_featured_homepage_reviews"),
    ]

    operations = [
        migrations.AddField(
            model_name="featuredhomepagereview",
            name="display_comment",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Gemini-polished quote shown on the homepage (falls back to the Google review if empty).",
            ),
        ),
    ]
