import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0245_message_moderation_and_ip_bans"),
    ]

    operations = [
        migrations.CreateModel(
            name="FeaturedHomepageReview",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "class_slug",
                    models.SlugField(
                        help_text="Deep-link target class slug for the reviewed business.",
                        max_length=255,
                    ),
                ),
                ("business_name", models.CharField(max_length=255)),
                (
                    "display_order",
                    models.PositiveSmallIntegerField(default=0),
                ),
                ("selected_at", models.DateTimeField(auto_now=True)),
                (
                    "selection_build_id",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        help_text="BUILD_ID/IMAGE_TAG/GIT_SHA of the deploy that selected this set.",
                        max_length=128,
                    ),
                ),
                (
                    "google_review",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="homepage_features",
                        to="quickstart.importedgooglereview",
                    ),
                ),
            ],
            options={
                "db_table": "featured_homepage_reviews",
                "ordering": ["display_order"],
            },
        ),
        migrations.AddConstraint(
            model_name="featuredhomepagereview",
            constraint=models.UniqueConstraint(
                fields=("google_review", "selection_build_id"),
                name="unique_featured_per_build",
            ),
        ),
        migrations.AddIndex(
            model_name="featuredhomepagereview",
            index=models.Index(
                fields=("selection_build_id", "display_order"),
                name="featured_build_order_idx",
            ),
        ),
    ]
