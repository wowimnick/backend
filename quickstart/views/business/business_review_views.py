import pytz
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import (
    PermissionDenied,
    ValidationError as DRFValidationError,
)
from django.db.models import Q, F, Count, Avg, DecimalField
from django.db.models.functions import Coalesce, Round, TruncDate
from datetime import timedelta, datetime
from django.utils import timezone
import logging
from decimal import Decimal

from quickstart.views.business.business_booking_views import BusinessBookingPagination
from quickstart.models import Reviews, BusinessInfo, ImportedGoogleReview
from quickstart.serializers.business.business_review_serializers import (
    BusinessReviewSerializer,
)
from quickstart.serializers.public.public_review_serializers import (
    ImportedGoogleReviewSerializer,
)

from quickstart.utils.permissions import IsAuthenticated, CanManageOwnBusinessReviews
from quickstart.utils.email_utils import send_review_response_notification_email

logger = logging.getLogger(__name__)


# --- ViewSet ---
class BusinessReviewViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BusinessReviewSerializer
    permission_classes = [IsAuthenticated, CanManageOwnBusinessReviews]
    pagination_class = BusinessBookingPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "comment",
        "userId__first_name",
        "userId__last_name",
        "userId__email",
        "classId__title",
        "rating",
        "status",
        "booking__user_facing_reference",
    ]
    ordering_fields = [
        "createdAt",
        "rating",
        "status",
        "classId__title",
        "reported_at",
        "responded_at",
    ]
    ordering = ["-createdAt"]
    http_method_names = ["get", "post", "head", "options"]

    def get_business_context(self):
        user = self.request.user
        try:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()
            if not business:
                raise PermissionDenied("User not associated with any managed business.")
            return business
        except BusinessInfo.DoesNotExist:
            raise PermissionDenied("Associated business not found.")

    def get_queryset(self):
        business = self.get_business_context()
        # This base queryset is for platform reviews, used by actions and list view.
        return (
            Reviews.objects.filter(classId__businessId=business)
            .select_related("userId", "classId", "booking")
            .distinct()
        )

    def list(self, request, *args, **kwargs):
        business = self.get_business_context()

        # Get query params for filtering
        rating_filter = request.query_params.get("rating")
        status_filter = request.query_params.get("status")
        search_query = request.query_params.get("search", "").strip()

        # 1. Get filtered platform reviews queryset
        platform_reviews_qs = self.filter_queryset(self.get_queryset())

        # Apply rating filter to platform reviews
        if rating_filter:
            platform_reviews_qs = platform_reviews_qs.filter(rating=int(rating_filter))

        # Apply status filter to platform reviews
        if status_filter:
            platform_reviews_qs = platform_reviews_qs.filter(status=status_filter)

        # 2. Get Google reviews and apply filters
        google_reviews_qs = business.imported_google_reviews.all()

        # Apply rating filter to Google reviews
        if rating_filter:
            google_reviews_qs = google_reviews_qs.filter(rating=int(rating_filter))

        # Apply search to Google reviews
        if search_query:
            google_reviews_qs = google_reviews_qs.filter(
                Q(comment__icontains=search_query)
                | Q(reviewer_name__icontains=search_query)
            )

        # 3. Combine and add metadata for sorting and serialization
        combined_list = [
            {"date": r.createdAt, "data": r, "type": "platform"}
            for r in platform_reviews_qs
        ] + [
            {"date": r.review_date, "data": r, "type": "google"}
            for r in google_reviews_qs
        ]
        # 4. Sort the combined list by date, newest first
        combined_list.sort(key=lambda x: x["date"], reverse=True)

        # 5. Manually paginate the sorted list
        paginator = self.pagination_class()
        paginated_list = paginator.paginate_queryset(combined_list, request, view=self)

        # 6. Serialize the data for the current page
        serialized_data = []
        for item in paginated_list:
            item_data = {}
            if item["type"] == "platform":
                item_data = self.get_serializer(item["data"]).data
            else:  # 'google'
                # Use the dedicated serializer for Google reviews
                item_data = ImportedGoogleReviewSerializer(
                    item["data"], context={"request": request}
                ).data
            # Add a 'source' field to the final output for the frontend
            item_data["source"] = item["type"]
            serialized_data.append(item_data)

        return paginator.get_paginated_response(serialized_data)

    @action(detail=False, methods=["get"], url_path="analytics")
    def analytics(self, request):
        business = self.get_business_context()
        start_date_str = request.query_params.get("start_date")
        end_date_str = request.query_params.get("end_date")

        # Default to all-time if no dates provided
        if not start_date_str or not end_date_str:
            # Get the earliest review date for this business
            earliest_platform = (
                Reviews.objects.filter(classId__businessId=business)
                .order_by("createdAt")
                .first()
            )
            earliest_google = (
                ImportedGoogleReview.objects.filter(business=business)
                .order_by("review_date")
                .first()
            )

            earliest_dates = []
            if earliest_platform:
                earliest_dates.append(earliest_platform.createdAt.date())
            if earliest_google:
                earliest_dates.append(earliest_google.review_date.date())

            start_date_naive = (
                min(earliest_dates)
                if earliest_dates
                else timezone.localdate() - timedelta(days=365)
            )
            end_date_naive = timezone.localdate()
        else:
            try:
                start_date_naive = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                end_date_naive = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                if start_date_naive > end_date_naive:
                    raise DRFValidationError("Start date cannot be after end date.")
            except ValueError:
                raise DRFValidationError("Invalid date format. Please use YYYY-MM-DD.")

        start_datetime_utc = timezone.make_aware(
            datetime.combine(start_date_naive, datetime.min.time()), pytz.utc
        )
        end_datetime_utc = timezone.make_aware(
            datetime.combine(end_date_naive, datetime.max.time()), pytz.utc
        )

        # --- Aggregate Platform Reviews ---
        platform_reviews_qs = Reviews.objects.filter(
            classId__businessId=business,
            createdAt__range=[start_datetime_utc, end_datetime_utc],
        )
        platform_stats = platform_reviews_qs.aggregate(
            total_reviews=Count("reviewId"),
            avg_rating=Avg("rating"),
            responded_count=Count(
                "business_response",
                filter=Q(business_response__isnull=False) & ~Q(business_response=""),
            ),
            under_review_count=Count("reviewId", filter=Q(status="under_review")),
            reported_count=Count("reviewId", filter=Q(reported=True)),
        )

        # --- Aggregate Google Reviews ---
        google_reviews_qs = ImportedGoogleReview.objects.filter(
            business=business, review_date__range=[start_datetime_utc, end_datetime_utc]
        )
        google_stats = google_reviews_qs.aggregate(
            total_reviews=Count("id"),
            avg_rating=Avg("rating"),
        )

        # --- Combine Metrics ---
        platform_total = platform_stats.get("total_reviews") or 0
        google_total = google_stats.get("total_reviews") or 0
        total_reviews = platform_total + google_total

        platform_total_rating = (platform_stats.get("avg_rating") or 0) * platform_total
        google_total_rating = (google_stats.get("avg_rating") or 0) * google_total

        if total_reviews > 0:
            combined_avg_rating = (
                platform_total_rating + google_total_rating
            ) / total_reviews
        else:
            combined_avg_rating = 0.0

        responded_count = platform_stats.get("responded_count") or 0
        google_responded_count = (
            google_reviews_qs.filter(owner_response__isnull=False)
            .exclude(owner_response="")
            .count()
        )

        total_responded = responded_count + google_responded_count
        response_rate = (
            (total_responded / total_reviews * 100) if total_reviews > 0 else 0.0
        )

        # --- Combine Rating Distribution ---
        platform_dist = platform_reviews_qs.values("rating").annotate(
            count=Count("rating")
        )
        google_dist = google_reviews_qs.values("rating").annotate(count=Count("rating"))

        combined_dist_map = {i: 0 for i in range(1, 6)}
        for item in platform_dist:
            combined_dist_map[item["rating"]] += item["count"]
        for item in google_dist:
            combined_dist_map[item["rating"]] += item["count"]

        rating_distribution = [
            {
                "name": f'{i} Star{"s" if i > 1 else ""}',
                "count": combined_dist_map.get(i, 0),
            }
            for i in range(1, 6)
        ]

        # --- Combine Reviews Over Time with intelligent aggregation ---
        business_pytz = pytz.timezone(business.business_timezone or "UTC")

        # Calculate date range in days
        date_range_days = (end_date_naive - start_date_naive).days

        # Determine aggregation level
        if date_range_days <= 60:
            # Daily for <= 2 months
            trunc_function = TruncDate
            date_format = "date"
        elif date_range_days <= 365:
            # Weekly for 2 months - 1 year
            from django.db.models.functions import TruncWeek

            trunc_function = TruncWeek
            date_format = "week"
        else:
            # Monthly for > 1 year
            from django.db.models.functions import TruncMonth

            trunc_function = TruncMonth
            date_format = "month"

        platform_daily = (
            platform_reviews_qs.annotate(
                date_local=trunc_function(F("createdAt"), tzinfo=business_pytz)
            )
            .values("date_local")
            .annotate(new_reviews=Count("reviewId"))
            .order_by("date_local")
        )
        google_daily = (
            google_reviews_qs.annotate(
                date_local=trunc_function(F("review_date"), tzinfo=business_pytz)
            )
            .values("date_local")
            .annotate(new_reviews=Count("id"))
            .order_by("date_local")
        )

        # Combine the aggregated data
        all_trend_dates_local = {}
        for trend in platform_daily:
            date_iso = trend["date_local"].isoformat()
            if date_iso not in all_trend_dates_local:
                all_trend_dates_local[date_iso] = {"date": date_iso, "new_reviews": 0}
            all_trend_dates_local[date_iso]["new_reviews"] += trend["new_reviews"]

        for trend in google_daily:
            date_iso = trend["date_local"].isoformat()
            if date_iso not in all_trend_dates_local:
                all_trend_dates_local[date_iso] = {"date": date_iso, "new_reviews": 0}
            all_trend_dates_local[date_iso]["new_reviews"] += trend["new_reviews"]

        processed_trends = sorted(
            all_trend_dates_local.values(), key=lambda x: x["date"]
        )

        analytics_data = {
            "summary_metrics": {
                "total_reviews_in_period": total_reviews,
                "platform_reviews_in_period": platform_total,
                "google_reviews_in_period": google_total,
                "average_rating_in_period": round(combined_avg_rating, 1),
                "response_rate_in_period": round(response_rate, 1),
                "reviews_under_review": platform_stats.get("under_review_count") or 0,
                "reviews_reported": platform_stats.get("reported_count") or 0,
            },
            "rating_distribution": rating_distribution,
            "response_status_distribution": [
                {"name": "Responded", "value": responded_count},
                {"name": "Not Responded", "value": platform_total - responded_count},
            ],
            "reviews_over_time": processed_trends,
            "date_range": {
                "start": start_date_naive.isoformat(),
                "end": end_date_naive.isoformat(),
                "aggregation": date_format,
            },
        }
        return Response(analytics_data)

    @action(detail=True, methods=["post"], url_path="respond")
    def respond(self, request, pk=None):
        review = self.get_object()  # Permission check done by get_object
        if not request.user.has_perm("quickstart.add_business_review_response"):
            raise PermissionDenied("You do not have permission to respond to reviews.")

        response_text = request.data.get("business_response", None)
        if response_text is None:  # Allow empty string to clear response
            raise DRFValidationError(
                {"business_response": "This field is required (can be empty to clear)."}
            )

        # Ensure response_text is a string, even if empty
        response_text_stripped = str(response_text).strip()

        if len(response_text_stripped) > 1000:  # Max length for response
            raise DRFValidationError(
                {"business_response": "Response cannot exceed 1000 characters."}
            )

        old_response = review.business_response
        review.business_response = (
            response_text_stripped if response_text_stripped else None
        )  # Store None if empty

        update_fields = ["business_response"]
        if (
            review.business_response != old_response
        ):  # Only update responded_at if response changed
            review.responded_at = timezone.now() if review.business_response else None
            update_fields.append("responded_at")

        review.save(update_fields=update_fields)
        logger.info(f"Business user {request.user.email} responded to Review {pk}.")

        if review.userId and review.business_response:
            try:
                send_review_response_notification_email(review.userId, review)
                logger.info(
                    f"Review response notification email task initiated for review {pk} to user {review.userId.email}."
                )
            except Exception as email_error:
                logger.error(
                    f"Failed to send review response notification for review {pk}: {email_error}",
                    exc_info=True,
                )

        serializer = self.get_serializer(review)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="report")
    def report(self, request, pk=None):
        review = self.get_object()  # Permission check
        report_reason = request.data.get("report_reason", "").strip()
        if not report_reason:
            raise DRFValidationError(
                {"report_reason": "A reason for reporting is required."}
            )
        if len(report_reason) > 500:  # Max length for reason
            raise DRFValidationError(
                {"report_reason": "Report reason cannot exceed 500 characters."}
            )

        if review.reported:
            return Response(
                {"message": "This review has already been reported."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        review.reported = True
        review.report_reason = report_reason
        review.reported_at = timezone.now()
        review.status = "under_review"  # Automatically change status
        review.save(
            update_fields=["reported", "report_reason", "reported_at", "status"]
        )
        logger.info(
            f"Review {pk} reported by business user {request.user.email}. Reason: {report_reason}"
        )

        # Placeholder for notifying admins
        # send_admin_review_reported_notification(review)

        serializer = self.get_serializer(review)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
