# Denormalized review aggregates for public class query performance

from decimal import Decimal

from django.db import migrations, models
from django.db.models import Avg, Count


def backfill_review_denorm(apps, schema_editor):
    ClassesMain = apps.get_model("quickstart", "ClassesMain")
    BusinessInfo = apps.get_model("quickstart", "BusinessInfo")
    Reviews = apps.get_model("quickstart", "Reviews")
    ImportedGoogleReview = apps.get_model("quickstart", "ImportedGoogleReview")

    for row in (
        Reviews.objects.filter(status="approved")
        .values("classId_id")
        .annotate(c=Count("reviewId"), a=Avg("rating"))
        .iterator(chunk_size=500)
    ):
        cid = row["classId_id"]
        cnt = int(row["c"] or 0)
        avg = row["a"]
        avg_dec = (
            Decimal(str(round(float(avg), 2)))
            if avg is not None
            else Decimal("0.00")
        )
        ClassesMain.objects.filter(pk=cid).update(
            platform_review_count=cnt,
            platform_avg_rating=avg_dec,
        )

    for row in (
        ImportedGoogleReview.objects.values("business")
        .annotate(c=Count("id"), a=Avg("rating"))
        .iterator(chunk_size=500)
    ):
        bid = row["business"]
        cnt = int(row["c"] or 0)
        avg = row["a"]
        avg_dec = (
            Decimal(str(round(float(avg), 2)))
            if avg is not None
            else Decimal("0.00")
        )
        BusinessInfo.objects.filter(pk=bid).update(
            google_review_count=cnt,
            google_avg_rating=avg_dec,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0235_alter_corporateinquiry_company_size"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="google_review_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Denormalized count of ImportedGoogleReview rows for this business.",
            ),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="google_avg_rating",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Denormalized average rating from imported Google reviews.",
                max_digits=4,
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="platform_review_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Denormalized count of approved platform reviews for this class.",
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="platform_avg_rating",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Denormalized average rating (approved platform reviews only).",
                max_digits=4,
            ),
        ),
        migrations.AddIndex(
            model_name="reviews",
            index=models.Index(
                fields=["classId", "status"],
                name="reviews_classid_status_idx",
            ),
        ),
        migrations.RunPython(backfill_review_denorm, noop_reverse),
    ]
