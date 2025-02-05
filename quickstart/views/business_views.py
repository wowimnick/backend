from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.generics import RetrieveUpdateDestroyAPIView
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Sum, Count, Avg
from datetime import timedelta
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
import logging

from ..utils.permissions import check_user_role

from ..models import (
    BusinessInfo, Booking, ClassesMain, Reviews
)
from ..serializers import (
    BusinessInfoSerializer,
    BusinessStatsSerializer,
    ClassesMainSerializer,
    BusinessRegistrationSerializer
)
from ..utils.permissions import IsBusinessOwner, IsManager

logger = logging.getLogger(__name__)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def register_business(request):
    try:
        serializer = BusinessRegistrationSerializer(
            data=request.data,
            context={'request': request}
        )
        
        if serializer.is_valid():
            business = serializer.save()
            return Response({
                'status': 'success',
                'message': 'Business registered successfully',
                'businessId': business.businessId
            }, status=status.HTTP_201_CREATED)
        
        return Response({
            'status': 'error',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
        
    except Exception as e:
        logger.error(f"Error in business registration: {str(e)}")
        return Response({
            'status': 'error',
            'message': 'An unexpected error occurred'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_user_businesses(request):
    """Get businesses user can manage"""
    user = request.user
    
    if user.has_role(['Admin', 'Super Admin']):
        businesses = BusinessInfo.objects.all()
    else:
        businesses = BusinessInfo.objects.filter(
            Q(owner=user) |
            Q(managers=user)
        ).distinct()
        
    serializer = BusinessInfoSerializer(businesses, many=True)
    return Response(serializer.data)


class BusinessViewSet(viewsets.ModelViewSet):
    serializer_class = BusinessStatsSerializer
    permission_classes = [IsManager]

    def get_queryset(self):
        return BusinessInfo.objects.filter(
            Q(owner=self.request.user) |
            Q(managers=self.request.user)
        ).distinct()

    @action(detail=True, methods=['get'])
    def dashboard_stats(self, request, pk=None):
        business = self.get_object()
        serializer = BusinessStatsSerializer(business)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def revenue_over_time(self, request, pk=None):
        business = self.get_object()
        timeframe = request.query_params.get('timeframe', 'monthly')
        
        bookings = Booking.objects.filter(
            class_instance__businessId=business,
            status__name='Completed'
        )

        if timeframe == 'daily':
            days = 30
            start_date = timezone.now() - timedelta(days=days)
            revenue_data = bookings.filter(
                booking_date__gte=start_date
            ).values('booking_date').annotate(
                revenue=Sum('class_instance__classPrice')
            ).order_by('booking_date')
        else:
            months = 12
            start_date = timezone.now() - timedelta(days=months * 30)
            revenue_data = bookings.filter(
                booking_date__gte=start_date
            ).values('booking_date__month').annotate(
                revenue=Sum('class_instance__classPrice')
            ).order_by('booking_date__month')

        return Response(revenue_data)

    @action(detail=True, methods=['get'])
    def class_performance(self, request, pk=None):
        business = self.get_object()
        
        classes = ClassesMain.objects.filter(
            businessId=business
        ).annotate(
            booking_count=Count('booking'),
            revenue=Sum('classPrice'),
            review_count=Count('reviews'),
            average_rating=Avg('reviews__rating')
        )

        return Response({
            'class_id': class_obj.classId,
            'class_name': class_obj.className,
            'booking_count': class_obj.booking_count,
            'revenue': class_obj.revenue,
            'review_count': class_obj.review_count,
            'average_rating': class_obj.average_rating
        } for class_obj in classes)

class BusinessInfoDetail(RetrieveUpdateDestroyAPIView):
    queryset = BusinessInfo.objects.all()
    serializer_class = BusinessInfoSerializer

    def get_object(self):
        queryset = self.get_queryset()
        obj = get_object_or_404(queryset, businessId=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data
        
        related_classes = ClassesMain.objects.filter(businessId=instance)
        class_data = ClassesMainSerializer(related_classes, many=True).data
        data['classes'] = class_data
        
        return Response(data)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if 'businessImage' in request.FILES:
            instance.businessImage = request.FILES['businessImage']
            instance.save()

        return Response(serializer.data)

class BusinessInfoViewSet(viewsets.ModelViewSet):
    serializer_class = BusinessInfoSerializer
    queryset = BusinessInfo.objects.all()

    def get_permissions(self):
        """
        Instantiate and return the list of permissions that this view requires.
        """
        if self.action in ['list', 'retrieve']:
            # Allow anyone to view business listings and details
            permission_classes = [AllowAny]
        else:
            # Require authentication and proper roles for other actions
            permission_classes = [IsAuthenticated, IsBusinessOwner]
        return [permission() for permission in permission_classes]

    def get_queryset(self):
        """
        Return different querysets based on authentication status and user role
        """
        queryset = super().get_queryset()
        
        if not self.request.user.is_authenticated:
            # For anonymous users, only show active businesses
            return queryset.filter(isActive=True)
            
        if check_user_role(self.request.user, ['Admin', 'Super Admin']):
            # Admins can see everything
            return queryset
            
        # Business owners/managers see their own businesses
        return queryset.filter(
            Q(owner=self.request.user) |
            Q(managers=self.request.user)
        ).distinct()