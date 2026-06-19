import random
from rest_framework import status, permissions, generics
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.exceptions import (
    ValidationError as DRFValidationError,
    PermissionDenied,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.throttling import ScopedRateThrottle
from django.db import transaction
from django.db.models import Q
import logging

from quickstart.utils.email_utils import send_review_submission_confirmation_email
from quickstart.utils.revalidation import (
    trigger_nextjs_revalidation,
    trigger_multiple_revalidations,
)

# Adjust import paths
from quickstart.models import ImportedGoogleReview, Reviews, Booking
from quickstart.serializers.public.public_review_serializers import (
    FeaturedHomepageReviewSerializer,
    ImportedGoogleReviewSerializer,
    ReviewSubmissionSerializer,
    PublicReviewSerializer,
)

logger = logging.getLogger(__name__)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10  # Number of reviews per page
    page_size_query_param = (
        "page_size"  # Allows client to override page_size e.g. ?page_size=20
    )
    max_page_size = 100  # Max page size client can request


class ReviewSubmission(APIView):
    """
    API endpoint for authenticated users to submit reviews for completed bookings.
    Enforces one review per Course (booking group).
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sensitive"

    @transaction.atomic
    def post(self, request):
        serializer = ReviewSubmissionSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        booking_id = serializer.validated_data["booking_id"]

        try:
            booking = Booking.objects.select_related(
                "user",
                "schedule_instance__schedule__option__classId",
                "schedule_instance__schedule__option__classId__businessId",
            ).get(id=booking_id)
        except Booking.DoesNotExist:
            logger.warning(
                f"User {request.user.email} submitted review for non-existent booking ID {booking_id}."
            )
            raise DRFValidationError(
                {"booking_id": "Valid booking not found for this ID."}
            )

        if booking.user != request.user:
            raise PermissionDenied("You can only review your own completed bookings.")

        if booking.status != "completed":
            raise DRFValidationError(
                {"booking_id": "You can only review completed bookings."}
            )

        # --- 1. Direct Booking Check ---
        if Reviews.objects.filter(booking=booking).exists():
            raise DRFValidationError(
                {"booking_id": "A review has already been submitted for this booking."}
            )

        # --- 2. Course/Group Check (New Logic) ---
        # If this booking is part of a course, check if the user reviewed ANY booking in this group
        if booking.booking_group_id:
            already_reviewed_course = Reviews.objects.filter(
                booking__booking_group_id=booking.booking_group_id,
                userId=request.user
            ).exists()
            
            if already_reviewed_course:
                raise DRFValidationError(
                    {"booking_id": "You have already submitted a review for this course."}
                )

        # --- Create Review ---
        try:
            class_instance = booking.schedule_instance.schedule.option.classId
            business_instance = class_instance.businessId

            review = Reviews.objects.create(
                userId=request.user,
                businessId=business_instance,
                classId=class_instance,
                booking=booking,
                rating=serializer.validated_data["rating"],
                comment=serializer.validated_data["comment"],
                image=serializer.validated_data.get("image_s3_key"),
                status="approved",
            )

            try:
                business_instance.update_review_aggregates()
            except Exception as update_error:
                logger.error(
                    f"Error calling update_review_aggregates for Business {business_instance.businessId} after creating review {review.reviewId}: {update_error}",
                    exc_info=True,
                )

            logger.info(
                f"Review {review.reviewId} submitted by user {request.user.email} for booking {booking.id}"
            )

            try:
                send_review_submission_confirmation_email(request.user, review)
            except Exception as email_error:
                logger.error(
                    f"Failed to send review submission confirmation email for review {review.reviewId}: {email_error}",
                    exc_info=True,
                )

            try:
                class_slug = getattr(class_instance, "slug", None) or str(class_instance.classId)
                business_slug = getattr(business_instance, "slug", None) or str(business_instance.businessId)
                tags = [
                    "reviews",
                    f"class-{class_slug}",
                    f"business-{business_slug}",
                    f"class-{class_slug}-reviews",
                    f"business-{business_slug}-reviews",
                ]
                trigger_multiple_revalidations(tags=tags)
                if class_slug:
                    trigger_nextjs_revalidation(path=f"/classes/{class_slug}")
                if business_slug:
                    trigger_nextjs_revalidation(path=f"/business/{business_slug}")
            except Exception as reval_err:
                logger.warning("Revalidation after review submit failed: %s", reval_err)

            response_serializer = PublicReviewSerializer(
                review, context={"request": request}
            )
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(
                f"Error saving review for user {request.user.email}, booking {booking.id}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {"error": "An unexpected error occurred while saving the review."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class PlatformClassReviews(generics.ListAPIView):
    serializer_class = PublicReviewSerializer
    permission_classes = [AllowAny]
    pagination_class = StandardResultsSetPagination

    @method_decorator(cache_page(60 * 60 * 24))
    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)

    def get_queryset(self):
        try:
            class_id = int(self.kwargs.get("pk"))
        except (ValueError, TypeError):
            return Reviews.objects.none()
        return (
            Reviews.objects.filter(classId=class_id, status="approved")
            .select_related("userId")
            .order_by("-createdAt")
        )


class ImportedGoogleReviewsView(generics.ListAPIView):
    serializer_class = ImportedGoogleReviewSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    @method_decorator(cache_page(60 * 60 * 24))
    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)

    def get_queryset(self):
        try:
            business_id = int(self.kwargs.get("business_id"))
            sample_size = int(self.request.query_params.get("sample_size", 10))
        except (ValueError, TypeError):
            return ImportedGoogleReview.objects.none()

        all_reviews_qs = ImportedGoogleReview.objects.filter(business_id=business_id)
        all_reviews_list = list(all_reviews_qs)

        if len(all_reviews_list) <= sample_size:
            return all_reviews_list

        return random.sample(all_reviews_list, sample_size)


class FeaturedHomepageReviewsView(generics.ListAPIView):
    """
    Returns the Gemini-selected Google reviews shown on the homepage hero strip.

    Each build refreshes the selection via the `refresh_homepage_reviews`
    management command. If no selection exists yet (e.g. before the first
    build runs Gemini), falls back to a random sample of recent Google
    reviews joined to a representative class slug for each business, so the
    hero never renders empty.

    Cached for 1 hour.
    """

    serializer_class = FeaturedHomepageReviewSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    @method_decorator(cache_page(60 * 60))
    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)

    def list(self, request, *args, **kwargs):
        items = self.get_review_items()
        serializer = self.get_serializer(items, many=True)
        return Response(serializer.data)

    def get_review_items(self):
        from quickstart.models import FeaturedHomepageReview

        latest_build = (
            FeaturedHomepageReview.objects
            .order_by("-selected_at", "-selection_build_id")
            .values_list("selection_build_id", flat=True)
            .first()
        )
        if latest_build is not None:
            qs = (
                FeaturedHomepageReview.objects
                .filter(selection_build_id=latest_build)
                .select_related("google_review")
                .order_by("display_order")
            )
            if qs.exists():
                return list(qs)

        return self._fallback_queryset()

    def get_queryset(self):
        """Unused — items are resolved in get_review_items for list()."""
        from quickstart.models import FeaturedHomepageReview
        return FeaturedHomepageReview.objects.none()

    def _fallback_queryset(self):
        """Build synthetic FeaturedHomepageReview-like objects from random Google reviews."""
        from quickstart.models import ClassesMain, FeaturedHomepageReview, ImportedGoogleReview

        recent = list(
            ImportedGoogleReview.objects
            .filter(rating__gte=4)
            .exclude(comment__isnull=True)
            .exclude(comment="")
            .select_related("business")
            .order_by("-review_date", "-created_at")[:24]
        )
        # Pick a representative class slug per business.
        slug_by_business = {}
        for r in recent:
            if r.business_id in slug_by_business:
                continue
            cls = (
                ClassesMain.objects
                .filter(businessId_id=r.business_id, slug__isnull=False)
                .exclude(slug="")
                .order_by("-platform_review_count", "classId")
                .first()
            )
            if cls:
                slug_by_business[r.business_id] = cls.slug

        # Sample up to 6; prefer reviews with a linkable class.
        eligible = [r for r in recent if r.business_id in slug_by_business]
        if len(eligible) > 6:
            eligible = random.sample(eligible, 6)
        elif not eligible and recent:
            pool = recent[:12]
            eligible = random.sample(pool, min(6, len(pool)))

        # Construct in-memory FeaturedHomepageReview shells (not persisted).
        synthetic = []
        for idx, r in enumerate(eligible):
            shell = FeaturedHomepageReview(
                google_review=r,
                class_slug=slug_by_business.get(r.business_id) or "",
                business_name=r.business.businessName if r.business_id else "",
                display_order=idx,
            )
            # Avoid hitting DB for pk on unsaved shell — serializer only reads
            # through `google_review.*` and the flat fields we set above.
            shell.pk = f"fallback-{idx}"
            synthetic.append(shell)
        return synthetic