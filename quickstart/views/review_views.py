from ..utils.permissions import check_user_role
from ..models import Reviews, Booking
from django.db import transaction
from django.db.models import Q, OuterRef, Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from ..serializers import ReviewSubmissionSerializer, ReviewSerializer
from rest_framework import generics

import logging
logger = logging.getLogger(__name__)

class ReviewSubmission(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @transaction.atomic
    def post(self, request):
        try:
            # Get and validate booking
            booking_id = request.data.get('booking_id')
            if not booking_id:
                return Response(
                    {'error': 'Booking ID is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            booking = get_object_or_404(
                Booking.objects.select_related(
                    'schedule_instance__schedule__option__classId__businessId'
                ),
                id=booking_id,
                user=request.user,
                status='completed'
            )

            # Check if review already exists
            if hasattr(booking, 'review'):
                return Response(
                    {'error': 'Review already exists for this booking'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Create serializer with booking data
            serializer = ReviewSubmissionSerializer(data={
                'rating': request.data.get('rating'),
                'comment': request.data.get('comment'),
                'image': request.FILES.get('image')
            })
            
            if not serializer.is_valid():
                return Response(
                    serializer.errors,
                    status=status.HTTP_400_BAD_REQUEST
                )

            validated_data = {
                'rating': serializer.validated_data['rating'],
                'comment': serializer.validated_data['comment']
            }

            # Handle image separately
            image = serializer.validated_data.get('image')
            if image:
                validated_data['image'] = image

            # Create review with all related fields except classOption
            review = Reviews.objects.create(
                userId=request.user,
                businessId=booking.schedule_instance.schedule.option.classId.businessId,
                classId=booking.schedule_instance.schedule.option.classId,
                # Removed classOption field
                booking=booking,
                **validated_data  
            )

            # Update business review count
            review.businessId.update_total_reviews()

            return Response({
                'message': 'Review submitted successfully',
                'reviewId': review.reviewId
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error submitting review: {str(e)}", exc_info=True)
            return Response(
                {'error': 'Failed to submit review'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
class ClassReviews(generics.ListAPIView):
    serializer_class = ReviewSerializer
    
    def get_queryset(self):
        class_id = self.kwargs['pk']
        return Reviews.objects.filter(
            classId=class_id
        ).select_related(
            'userId'
        ).only(
            'reviewId',
            'rating',
            'comment',
            'image',
            'createdAt',
            'userId__first_name',
            'userId__last_name',
            'userId__avatar'
        ).order_by('-createdAt')