from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.generics import RetrieveUpdateDestroyAPIView
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Sum, Count, Avg
from datetime import timedelta

from ..models import (
    BusinessInfo, Booking, ClassesMain, Reviews
)
from ..serializers import (
    BusinessInfoSerializer,
    BusinessStatsSerializer,
    ClassesMainSerializer
)
from .permissions import IsBusinessOwner, IsManager

class BusinessViewSet(viewsets.ModelViewSet):
    serializer_class = BusinessStatsSerializer
    permission_classes = [IsManager]

    def get_queryset(self):
        return BusinessInfo.objects.filter(owner=self.request.user)

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
    queryset = BusinessInfo.objects.all()
    serializer_class = BusinessInfoSerializer

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsBusinessOwner]
        elif self.action in ['list', 'retrieve']:
            permission_classes = [IsManager]
        else:
            permission_classes = [permissions.IsAuthenticated]
        return [permission() for permission in permission_classes]