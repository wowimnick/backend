from django.shortcuts import get_object_or_404
from rest_framework import viewsets, filters, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
import logging

# Adjust import paths based on your project structure
from ...models import Booking, BusinessInfo
from ...serializers.public.public_business_serializers import PublicBusinessInfoSerializer, BusinessContactDetailSerializer 

logger = logging.getLogger(__name__)

class PublicBusinessInfoViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides READ-ONLY access to public Business Information.
    Filters to show only active and verified businesses.
    Supports searching and ordering.
    (URL Base: /api/businesses/)
    """
    serializer_class = PublicBusinessInfoSerializer
    permission_classes = [AllowAny] 

    # Add standard filtering/searching capabilities
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'businessName', 'businessDescription', 'businessCity',
        'businessState', 'classCategory', 'subcategories' # Add fields you want searchable
    ]
    ordering_fields = ['businessName', 'createdAt', 'featured'] # Add fields you want orderable
    ordering = ['-featured', 'businessName'] # Default ordering: featured first, then by name

    # --- DEFINE get_queryset method ---
    def get_queryset(self):
        """
        Returns a queryset containing only active and verified businesses.
        Applies the default ordering.
        """
        logger.debug("Fetching public business info queryset (active & verified)")
        queryset = BusinessInfo.objects.select_related(
            'owner', 'classCategory'
        ).filter(
            isActive=True,
            verificationStatus='verified'
        )
        return queryset
    
    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated])
    def contact_details(self, request, pk=None):
        """
        An authenticated action to reveal business contact details
        ONLY to users who have a confirmed or completed booking.
        """
        user = request.user
        business = self.get_object() # Gets the business using the ViewSet's logic

        # Check if a booking exists that links the user to this business.
        has_booking = Booking.objects.filter(
            user=user,
            schedule_instance__schedule__option__classId__businessId=business,
            status__in=['confirmed', 'completed']
        ).exists()

        if not has_booking:
            # If the user has no valid booking, deny permission.
            return Response(
                {'detail': 'You must have a booking with this business to view contact details.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # If they have a booking, serialize with the secure contact serializer and return.
        serializer = BusinessContactDetailSerializer(business)
        return Response(serializer.data, status=status.HTTP_200_OK)