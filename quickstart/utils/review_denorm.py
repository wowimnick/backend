"""Keep denormalized review aggregates in sync (ClassesMain + BusinessInfo)."""
from decimal import Decimal

from django.db.models import Avg, Count

from quickstart.models import BusinessInfo, ClassesMain, ImportedGoogleReview, Reviews


def refresh_platform_review_aggregates_for_class(class_pk) -> None:
    """Recompute approved platform review count + avg for a class (O(1) update)."""
    agg = Reviews.objects.filter(classId_id=class_pk, status="approved").aggregate(
        c=Count("reviewId"), a=Avg("rating")
    )
    cnt = int(agg["c"] or 0)
    avg = agg["a"]
    avg_dec = (
        Decimal(str(round(float(avg), 2)))
        if avg is not None
        else Decimal("0.00")
    )
    ClassesMain.objects.filter(pk=class_pk).update(
        platform_review_count=cnt,
        platform_avg_rating=avg_dec,
    )


def refresh_google_review_aggregates_for_business(business_pk) -> None:
    """Recompute imported Google review count + avg for a business."""
    agg = ImportedGoogleReview.objects.filter(business_id=business_pk).aggregate(
        c=Count("id"), a=Avg("rating")
    )
    cnt = int(agg["c"] or 0)
    avg = agg["a"]
    avg_dec = (
        Decimal(str(round(float(avg), 2)))
        if avg is not None
        else Decimal("0.00")
    )
    BusinessInfo.objects.filter(pk=business_pk).update(
        google_review_count=cnt,
        google_avg_rating=avg_dec,
    )
