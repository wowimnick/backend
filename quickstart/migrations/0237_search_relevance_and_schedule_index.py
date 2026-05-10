# Generated manually for Airbnb-fast search rollout

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0236_review_denorm_and_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="classesmain",
            name="search_relevance_score",
            field=models.FloatField(
                db_index=True,
                default=0.0,
                help_text="Denormalized ranking score for public explore (Typesense + DB list).",
            ),
        ),
        migrations.AddIndex(
            model_name="schedule",
            index=models.Index(
                fields=["option", "date", "price"], name="schedules_opt_date_price_idx"
            ),
        ),
    ]
