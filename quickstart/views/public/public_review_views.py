from rest_framework import status, permissions, generics
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import (
    ValidationError as DRFValidationError,
    PermissionDenied,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.throttling import ScopedRateThrottle
from django.db import transaction
import logging

from quickstart.utils.email_utils import send_review_submission_confirmation_email

# Adjust import paths
from quickstart.models import Reviews, Booking
from quickstart.serializers.public.public_review_serializers import (
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


# In public_review_views.py


class ReviewSubmission(APIView):
    """
    API endpoint for authenticated users to submit reviews for completed bookings.
    MODIFIED to use JSON payload with an S3 key for the image.
    """

    permission_classes = [IsAuthenticated]
    # MODIFIED: Changed parsers to primarily handle JSON.
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    # --- Rate Limiting ---
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sensitive"

    @transaction.atomic
    def post(self, request):
        # The request data is now expected to be JSON.
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

        # --- (All validation logic for booking ownership and status remains the same) ---
        if booking.user != request.user:
            raise PermissionDenied("You can only review your own completed bookings.")

        if booking.status != "completed":
            raise DRFValidationError(
                {"booking_id": "You can only review completed bookings."}
            )

        if Reviews.objects.filter(booking=booking).exists():
            raise DRFValidationError(
                {"booking_id": "A review has already been submitted for this booking."}
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
                # MODIFIED: Get the S3 key from validated_data
                image=serializer.validated_data.get("image_s3_key"),
                status="approved",
            )

            # --- (The rest of the view logic remains the same) ---

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
                logger.info(
                    f"Review submission confirmation email prepared/queued for review {review.reviewId}"
                )
            except Exception as email_error:
                logger.error(
                    f"Failed to send review submission confirmation email for review {review.reviewId}: {email_error}",
                    exc_info=True,
                )

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


class ClassReviews(generics.ListAPIView):
    """
    API endpoint for listing approved reviews for a specific class.
    (PUBLIC CONTEXT)
    """

    serializer_class = PublicReviewSerializer
    permission_classes = [AllowAny]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        try:
            class_id = int(self.kwargs.get("pk"))
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid class ID format in ClassReviews URL: {self.kwargs.get('pk')}"
            )
            return Reviews.objects.none()

        return (
            Reviews.objects.filter(classId=class_id, status="approved")
            .select_related("userId")
            .order_by("-createdAt")
        )
