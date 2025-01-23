# Django imports
from django.db import models
from django.db.models import Q, Sum, Count, Avg, F
from django.db.models.functions import TruncDate, ExtractWeekDay, datetime
from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import datetime, timedelta

# REST Framework imports
from rest_framework import viewsets, status, permissions, views
from rest_framework.decorators import action, api_view, permission_classes, parser_classes
from rest_framework.generics import RetrieveUpdateDestroyAPIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied

# Local imports
from .permissions import check_user_role, IsBusinessOwner, IsManager
from ..models import (
    BusinessInfo,
    Booking,
    ClassesMain,
    Reviews,
    Schedule
)
from ..serializers import (
    BusinessInfoSerializer,
    BusinessStatsSerializer,
    ClassesMainSerializer,
    BusinessRegistrationSerializer,
    BookingCreateSerializer,
    BookingDetailSerializer
)

# Python standard library
import logging
from datetime import timedelta

class BookingViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        user = self.request.user
        status_filter = self.request.query_params.get('status')
        
        # Start with base queryset
        queryset = Booking.objects.select_related(
            'schedule_instance', 
            'schedule_instance__schedule',
            'schedule_instance__schedule__option',
            'schedule_instance__schedule__option__classId',
            'schedule_instance__schedule__option__classId__businessId',
            'student'
        )
        
        # Handle multiple status filtering
        if status_filter:
            status_values = [s.strip() for s in status_filter.split(',')]
            queryset = queryset.filter(status__in=status_values)
        
        # For business owners/managers - show all bookings for their business
        if check_user_role(user, ['Business Owner', 'Manager']):
            return queryset.filter(
                Q(schedule_instance__schedule__option__classId__businessId__owner=user) |
                Q(schedule_instance__schedule__option__classId__businessId__managers=user)
            ).distinct()
                
        # For students - show only their bookings
        return queryset.filter(student__user=user)

    def get_serializer_class(self):
        if self.action == 'create':
            return BookingCreateSerializer
        return BookingDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            booking = serializer.save(
                student=request.user.student_profile,
                status='pending',
                payment_status='pending'
            )
            
            # Here you would integrate with payment processing
            # For now, we'll just confirm the booking
            booking.status = 'confirmed'
            booking.payment_status = 'paid'
            booking.save()
            
            return Response(
                BookingDetailSerializer(booking).data,
                status=status.HTTP_201_CREATED
            )
            
        except ValidationError as e:
            return Response(
                {'error': e.messages},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        
        # Can only cancel confirmed bookings
        if booking.status != 'confirmed':
            return Response(
                {'error': ['Can only cancel confirmed bookings']},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        booking.cancellation_reason = request.data.get('reason', '')
        booking.save()
        
        return Response(BookingDetailSerializer(booking).data)

    @action(detail=False, methods=['get'])
    def schedule_availability(self, request):
        """Get availability for a specific schedule"""
        schedule_id = request.query_params.get('schedule_id')
        if not schedule_id:
            return Response(
                {'error': ['schedule_id is required']},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        try:
            schedule = Schedule.objects.get(id=schedule_id)
            current_bookings = schedule.current_bookings
            
            return Response({
                'schedule_id': schedule_id,
                'total_capacity': schedule.effective_max_participants,
                'booked': current_bookings,
                'available': schedule.effective_max_participants - current_bookings,
                'is_full': current_bookings >= schedule.effective_max_participants
            })
            
        except Schedule.DoesNotExist:
            return Response(
                {'error': ['Schedule not found']},
                status=status.HTTP_404_NOT_FOUND
            )
        
    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """Get analytics for bookings"""
        try:
            # Get business and date range
            business = self._get_business(request.user)
            start_date, end_date = self._get_date_range(request)
            
            # Get base queryset
            bookings = self._get_analytics_queryset(business, start_date, end_date)
            
            # Compile response data
            data = {
                'summary': self._get_summary_metrics(bookings),
                'trends': self._get_booking_trends(bookings),
                'by_day': self._get_weekday_distribution(bookings),
                'by_type': self._get_class_type_distribution(bookings)
            }
            
            return Response(data)
            
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _get_business(self, user):
        """Get the business associated with the user"""
        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(managers=user)
        ).first()
        
        if not business and not check_user_role(user, ['Admin', 'Super Admin']):
            raise PermissionDenied("No associated business found")
            
        return business

    def _get_date_range(self, request):
        """Get and validate date range from request parameters"""
        try:
            start_date = request.query_params.get('start_date')
            end_date = request.query_params.get('end_date')
            
            if start_date and end_date:
                start_date = timezone.make_aware(datetime.strptime(start_date, '%Y-%m-%d'))
                end_date = timezone.make_aware(datetime.strptime(end_date, '%Y-%m-%d'))
            else:
                # Default to last 30 days
                end_date = timezone.now()
                start_date = end_date - timedelta(days=30)
            
            return start_date.date(), end_date.date()
            
        except ValueError as e:
            raise ValidationError("Invalid date format. Use YYYY-MM-DD")

    def _get_analytics_queryset(self, business, start_date, end_date):
        """Get base queryset for analytics with filters applied"""
        return Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            booking_date__range=[start_date, end_date],
            status__in=['confirmed', 'completed'] 
        ).select_related(
            'schedule_instance__schedule__option__classId'
        )

    def _get_summary_metrics(self, queryset):
        """Calculate summary metrics"""
        daily_counts = queryset.annotate(
            date=TruncDate('booking_date')
        ).values('date').annotate(
            count=Count('id')
        )
        
        total_bookings = queryset.count()
        avg_daily = daily_counts.aggregate(avg=Avg('count'))['avg'] or 0
        
        # Get most popular day
        weekday_counts = queryset.annotate(
            weekday=ExtractWeekDay('booking_date')
        ).values('weekday').annotate(
            count=Count('id')
        ).order_by('-count')
        
        weekday_map = {
            1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday',
            5: 'Thursday', 6: 'Friday', 7: 'Saturday'
        }
        
        most_popular_day = weekday_map[weekday_counts[0]['weekday']] if weekday_counts else 'N/A'
        
        return {
            'total_bookings': total_bookings,
            'average_daily': round(avg_daily, 1),
            'most_popular_day': most_popular_day
        }

    def _get_booking_trends(self, queryset):
        """Get daily booking trends"""
        return queryset.annotate(
            date=TruncDate('booking_date')
        ).values('date').annotate(
            bookings=Count('id')
        ).order_by('date')

    def _get_weekday_distribution(self, queryset):
        """Get booking distribution by day of week"""
        day_to_number = {
            'Sun': 1, 'Mon': 2, 'Tue': 3, 'Wed': 4,
            'Thu': 5, 'Fri': 6, 'Sat': 7
        }
        
        weekday_map = {
            1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday',
            5: 'Thursday', 6: 'Friday', 7: 'Saturday'
        }
        
        distribution = queryset.values(
            'schedule_instance__schedule__day'
        ).annotate(
            bookings=Count('id')
        ).order_by('schedule_instance__schedule__day')
        
        # Convert the distribution to the expected format
        return [
            {
                'day': weekday_map[day_to_number[day['schedule_instance__schedule__day']]],
                'bookings': day['bookings']
            }
            for day in distribution
        ]

    def _get_class_type_distribution(self, queryset):
        """Get booking distribution by class type"""
        return queryset.values(
            type=F('schedule_instance__schedule__option__classId__category')
        ).annotate(
            bookings=Count('id')
        ).order_by('-bookings')