from rest_framework import status, permissions, generics
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError as DRFValidationError, PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404
import logging

# Adjust import paths
from ...models import Reviews, Booking
from ...serializers.public.public_review_serializers import (
    ReviewSubmissionSerializer, PublicReviewSerializer
)

logger = logging.getLogger(__name__)

class ReviewSubmission(APIView):
    """
    API endpoint for authenticated users to submit reviews for completed bookings.
    (PUBLIC/STUDENT CONTEXT)
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @transaction.atomic
    def post(self, request):
        # Validate the incoming data using the updated serializer
        serializer = ReviewSubmissionSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True) # Will raise 400 if basic validation fails

        # --- Get booking_id from VALIDATED data ---
        booking_id = serializer.validated_data['booking_id']

        # --- Fetch the booking using the validated ID ---
        try:
            # Fetch the booking and related objects needed for creation/validation
            booking = Booking.objects.select_related(
                'user', # Needed for ownership check
                'schedule_instance__schedule__option__classId__businessId' # Needed for review creation
            ).get(id=booking_id)
        except Booking.DoesNotExist:
            logger.warning(f"User {request.user.email} submitted review for non-existent booking ID {booking_id}.")
            # Raise validation error linked to the input field
            raise DRFValidationError({'booking_id': 'Valid booking not found for this ID.'})

        # --- Validation: Ensure booking belongs to user and is reviewable ---
        if booking.user != request.user:
            logger.warning(f"User {request.user.email} attempted to review booking {booking.id} owned by {booking.user.email}.")
            # Use PermissionDenied for authorization issues
            raise PermissionDenied("You can only review your own completed bookings.")

        if booking.status != 'completed':
            logger.warning(f"User {request.user.email} attempted to review non-completed booking {booking.id} (Status: {booking.status}).")
            # Use DRFValidationError for business logic validation failures linked to input
            raise DRFValidationError({'booking_id': 'You can only review completed bookings.'})

        # Check if review already exists for this booking
        if Reviews.objects.filter(booking=booking).exists():
            logger.warning(f"User {request.user.email} attempted duplicate review for booking {booking.id}.")
            raise DRFValidationError({'booking_id': 'A review has already been submitted for this booking.'})

        # --- Create Review ---
        try:
            # Extract necessary related objects
            class_instance = booking.schedule_instance.schedule.option.classId
            business_instance = class_instance.businessId

            review = Reviews.objects.create(
                userId=request.user,              # The user submitting
                businessId=business_instance,     # Link to business
                classId=class_instance,           # Link to class
                booking=booking,                  # Link to the specific booking
                rating=serializer.validated_data['rating'],
                comment=serializer.validated_data['comment'],
                image=serializer.validated_data.get('image'), # Handles optional image
                status='approved'                 # Default to approved (or 'under_review')
            )

            # Update business review count safely
            if hasattr(review.businessId, 'update_total_reviews'):
                review.businessId.update_total_reviews()
            else:
                # Log an error if the method is missing, but don't crash the request
                logger.error(f"BusinessInfo model missing 'update_total_reviews' method. Review {review.reviewId} created, but count not updated.")

            logger.info(f"Review {review.reviewId} submitted by user {request.user.email} for booking {booking.id}")

            # Return success response using the public serializer
            response_serializer = PublicReviewSerializer(review, context={'request': request})
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            # Catch potential errors during object creation or fetching related instances
            logger.error(f"Error saving review for user {request.user.email}, booking {booking.id}: {str(e)}", exc_info=True)
            # Return a generic 500 error for server-side issues during creation
            return Response(
                {'error': 'An unexpected error occurred while saving the review.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class ClassReviews(generics.ListAPIView):
    """
    API endpoint for listing approved reviews for a specific class.
    (PUBLIC CONTEXT)
    """
    serializer_class = PublicReviewSerializer
    permission_classes = [AllowAny]
    # pagination_class = ... # Add pagination if needed

    def get_queryset(self):
        try:
            class_id = int(self.kwargs.get('pk'))
        except (ValueError, TypeError):
            logger.warning(f"Invalid class ID format in ClassReviews URL: {self.kwargs.get('pk')}")
            return Reviews.objects.none() # Return empty for invalid ID

        # Filter public, approved reviews for the class
        return Reviews.objects.filter(
            classId=class_id,
            status='approved'
        ).select_related(
            'userId' # Optimize user fetching
        ).order_by('-createdAt')