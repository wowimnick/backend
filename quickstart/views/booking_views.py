# Django imports
import uuid
from django.db import models, transaction
from django.db.models import Q, Sum, Count, Avg, F, Prefetch, Window, Value, FloatField, ExpressionWrapper
from django.db.models.functions import TruncDate, ExtractWeekDay, datetime, Concat, RowNumber, Cast, ExtractHour
from django.utils import timezone
from datetime import datetime, timedelta
from rest_framework.pagination import PageNumberPagination

# REST Framework imports
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied

# Local imports
from ..utils.permissions import check_user_role
from ..models import (
    BusinessInfo,
    Booking,
    ScheduleInstance,
)
from ..serializers import (
    BookingCreateSerializer,
    BookingDetailSerializer,
    BookingListSerializer,
    StudentBookingSerializer
)

# Python standard library
import logging

logger = logging.getLogger(__name__)
from datetime import timedelta

class BookingPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

class BookingViewSet(viewsets.ModelViewSet):
    pagination_class = BookingPagination

    def get_serializer_class(self):
        if self.action == 'list':
            return BookingListSerializer
        if self.action == 'create':
            return BookingCreateSerializer
        return BookingDetailSerializer
    
    def get_queryset(self):
        queryset = Booking.objects.select_related(
            'schedule_instance__schedule__option__classId__businessId',
            'user'
        )

        # Apply filters
        filters = {}
        
        # Handle status filter
        if self.request.query_params.get('status'):
            status_values = self.request.query_params['status'].split(',')
            queryset = queryset.filter(status__in=status_values)
        else:
            queryset = queryset.filter(status='confirmed')
        
        # Apply other filters
        if self.request.query_params.get('user_email'):
            filters['user__email'] = self.request.query_params['user_email']
        if self.request.query_params.get('class_name'):
            filters['schedule_instance__schedule__option__classId__title__icontains'] = (
                self.request.query_params['class_name']
            )
                
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date and end_date:
            filters['schedule_instance__date__range'] = [start_date, end_date]

        # Apply search if provided
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(user__email__icontains=search) |
                Q(schedule_instance__schedule__option__classId__title__icontains=search)
            )

        # Apply ordering
        ordering = self.request.query_params.get('ordering')
        if ordering:
            order_fields = {
                'date': 'schedule_instance__date',
                '-date': '-schedule_instance__date',
                'user_name': 'user__first_name',
                '-user_name': '-user__first_name',
                'class_name': 'schedule_instance__schedule__option__classId__title',
                '-class_name': '-schedule_instance__schedule__option__classId__title'
            }
            
            if ordering in order_fields:
                queryset = queryset.order_by(order_fields[ordering])

        return queryset.filter(**filters)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        
        # Cache completed and cancelled counts using efficient queries
        if not hasattr(self, '_total_completed'):
            self._total_completed = (
                Booking.objects
                .filter(status='completed')
                .count()
            )
        if not hasattr(self, '_total_cancelled'):
            self._total_cancelled = (
                Booking.objects
                .filter(status='cancelled')
                .count()
            )
        
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            response = self.get_paginated_response(serializer.data)
            response.data['total_completed'] = self._total_completed
            response.data['total_cancelled'] = self._total_cancelled
            return response

        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'results': serializer.data,
            'count': queryset.count(),
            'total_completed': self._total_completed,
            'total_cancelled': self._total_cancelled
        })

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

    @action(detail=False, methods=['post'])
    def check_availability(self, request):
        """Check availability for a specific schedule instance"""
        instance_id = request.data.get('schedule_instance_id')
        participants = request.data.get('participants', 1)
        
        try:
            instance = ScheduleInstance.objects.get(id=instance_id)
            
            if not instance.can_accommodate(participants):
                return Response({
                    'available': False,
                    'message': 'Not enough spots available'
                })
                
            return Response({
                'available': True,
                'message': 'Spots available'
            })
            
        except ScheduleInstance.DoesNotExist:
            return Response(
                {'error': 'Invalid schedule instance'},
                status=status.HTTP_404_NOT_FOUND
            )

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        
        if booking.status != 'confirmed':
            return Response(
                {'error': 'Can only cancel confirmed bookings'},
                status=status.HTTP_400_BAD_REQUEST
            )

        cancellation_policy = booking.schedule_instance.schedule.option.cancellationPolicy
        
        current_time = timezone.now()
        class_time = timezone.make_aware(
            timezone.datetime.combine(
                booking.schedule_instance.date,
                booking.schedule_instance.time
            )
        )
        
        hours_until_class = (class_time - current_time).total_seconds() / 3600

        policy_hours = {
            '24h': 24,
            '48h': 48,
            '72h': 72,
            'flexible': float('inf')
        }

        if hours_until_class < policy_hours.get(cancellation_policy, 0):
            return Response({
                'error': f'Cancellation not allowed within {cancellation_policy} of class start'
            }, status=status.HTTP_400_BAD_REQUEST)

        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        booking.cancellation_reason = request.data.get('reason', '')
        booking.save()
        
        return Response(BookingDetailSerializer(booking).data)

    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """Get comprehensive booking analytics"""
        try:
            # Get business and date range
            business = self._get_business(request.user)
            start_date, end_date = self._get_date_range(request)
            
            # Get base queryset including all relevant statuses
            bookings = (
                Booking.objects.filter(
                    schedule_instance__schedule__option__classId__businessId=business,
                    booking_date__range=[start_date, end_date]
                )
                .select_related(
                    'schedule_instance__schedule__option__classId',
                    'user'  # Changed from student to user
                )
            )
            
            # Basic counts
            total_bookings = bookings.count()
            cancelled_bookings = bookings.filter(status='cancelled').count()
            completed_bookings = bookings.filter(status='completed').count()
            confirmed_bookings = bookings.filter(status='confirmed').count()
            
            # Calculate rates
            cancellation_rate = (
                (cancelled_bookings / total_bookings) * 100
                if total_bookings > 0 else 0
            )
            
            # Attendance tracking
            completed_with_attendance = bookings.filter(
                status='completed',
                attendance_marked=True
            ).count()
            
            attended_bookings = bookings.filter(
                status='completed',
                attendance_marked=True,
                attended=True
            ).count()
            
            attendance_rate = (
                (attended_bookings / completed_with_attendance) * 100
                if completed_with_attendance > 0 else 0
            )

            # Calculate daily trends
            daily_trends = (
                bookings
                .annotate(date=TruncDate('booking_date'))
                .values('date')
                .annotate(
                    new_bookings=Count('id'),
                    cancelled_count=Count('id', filter=Q(status='cancelled')),
                    total_count=Count('id')
                )
                .order_by('date')
            )

            processed_trends = []
            for trend in daily_trends:
                cancellation_rate = (
                    (trend['cancelled_count'] / trend['total_count']) * 100
                    if trend['total_count'] > 0 else 0
                )
                processed_trends.append({
                    'date': trend['date'].isoformat(),
                    'new_bookings': trend['new_bookings'],
                    'cancellation_rate': round(cancellation_rate, 1)
                })

            # Calculate class occupancy
            class_occupancy = []
            raw_occupancy = (
                bookings
                .values(
                    'schedule_instance__schedule__option__classId__title'
                )
                .annotate(
                    total_capacity=Sum('schedule_instance__max_participants'),
                    actual_bookings=Count('id'),
                    cancelled=Count('id', filter=Q(status='cancelled'))
                )
                .order_by('-actual_bookings')
            )
            
            for entry in raw_occupancy:
                occupancy_rate = (
                    (entry['actual_bookings'] - entry['cancelled']) / entry['total_capacity'] * 100
                    if entry['total_capacity'] and entry['total_capacity'] > 0
                    else 0
                )
                class_occupancy.append({
                    'class_name': entry['schedule_instance__schedule__option__classId__title'],
                    'occupancy_rate': round(occupancy_rate, 1),
                    'total_bookings': entry['actual_bookings'],
                    'cancelled': entry['cancelled']
                })

            # Time distribution analysis
            time_distribution = (
                bookings
                .annotate(hour=ExtractHour('schedule_instance__time'))
                .values('hour')
                .annotate(
                    bookings=Count('id'),
                    cancelled=Count('id', filter=Q(status='cancelled')),
                    completed=Count('id', filter=Q(status='completed'))
                )
                .order_by('hour')
            )

            # Booking type distribution
            booking_types = []
            raw_types = (
                bookings
                .values('enrollment_type')
                .annotate(
                    count=Count('id'),
                    cancelled=Count('id', filter=Q(status='cancelled')),
                    completed=Count('id', filter=Q(status='completed'))
                )
                .order_by('-count')
            )

            for entry in raw_types:
                percentage = (
                    (entry['count'] / total_bookings) * 100
                    if total_bookings > 0 else 0
                )
                booking_types.append({
                    'type': entry['enrollment_type'],
                    'count': entry['count'],
                    'cancelled': entry['cancelled'],
                    'completed': entry['completed'],
                    'percentage': round(percentage, 1)
                })

            # User retention analysis (previously student retention)
            user_bookings = (
                bookings
                .values('user')  # Changed from student to user
                .annotate(booking_count=Count('id'))
            )
            
            total_users = user_bookings.count()  # Changed from total_students
            repeat_users = user_bookings.filter(booking_count__gt=1).count()  # Changed from repeat_students
            
            retention_rate = (
                (repeat_users / total_users) * 100  # Changed variable names
                if total_users > 0 else 0
            )

            # Popular classes analysis
            popular_classes = []
            raw_popular = (
                bookings
                .values(
                    'schedule_instance__schedule__option__classId__title'
                )
                .annotate(
                    total_bookings=Count('id'),
                    unique_users=Count('user', distinct=True),  # Changed from student to user
                    cancelled=Count('id', filter=Q(status='cancelled')),
                    completed=Count('id', filter=Q(status='completed')),
                    attended=Count('id', filter=Q(status='completed', attended=True))
                )
                .order_by('-total_bookings')[:5]
            )

            for entry in raw_popular:
                cancellation_rate = (
                    (entry['cancelled'] / entry['total_bookings']) * 100
                    if entry['total_bookings'] > 0 else 0
                )
                attendance_rate = (
                    (entry['attended'] / entry['completed']) * 100
                    if entry['completed'] > 0 else 0
                )
                popular_classes.append({
                    'class_name': entry['schedule_instance__schedule__option__classId__title'],
                    'total_bookings': entry['total_bookings'],
                    'unique_users': entry['unique_users'],  # Changed from unique_students
                    'cancellation_rate': round(cancellation_rate, 1),
                    'attendance_rate': round(attendance_rate, 1)
                })

            # Prepare response
            response_data = {
                'summary': {
                    'total_bookings': total_bookings,
                    'active_bookings': confirmed_bookings,
                    'completed_bookings': completed_bookings,
                    'cancelled_bookings': cancelled_bookings,
                    'cancellation_rate': round(cancellation_rate, 1),
                    'attendance_rate': round(attendance_rate, 1),
                    'user_retention_rate': round(retention_rate, 1)  # Changed from student_retention_rate
                },
                'trends': processed_trends,
                'class_insights': {
                    'occupancy_rates': class_occupancy,
                    'popular_classes': popular_classes
                },
                'booking_patterns': {
                    'time_distribution': [{
                        'hour': entry['hour'],
                        'bookings': entry['bookings'],
                        'cancelled': entry['cancelled'],
                        'completed': entry['completed']
                    } for entry in time_distribution],
                    'booking_types': booking_types
                }
            }
            
            return Response(response_data)
                
        except Exception as e:
            logger.exception("Error in booking analytics")
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

    def _get_enrollment_distribution(self, queryset):
        """Get booking distribution by enrollment type"""
        return queryset.values(
            'enrollment_type'
        ).annotate(
            bookings=Count('id'),
            revenue=Sum('amount_paid')
        ).order_by('-bookings')
    
    ################################################################################################
    # Student booking views
    ################################################################################################
    
    @action(detail=False, methods=['get'])
    def my_bookings(self, request):
        """Get the current user's bookings"""
        status_filter = request.query_params.get('status', 'confirmed')
        
        queryset = self.get_queryset().filter(
            user=request.user,
            status=status_filter
        ).select_related(
            'schedule_instance__schedule__option__classId__businessId',
            'review'  # Add this to efficiently load review status
        ).prefetch_related(
            'schedule_instance__schedule__option__classId__images'
        ).order_by('schedule_instance__date', 'schedule_instance__time')

        serializer = StudentBookingSerializer(queryset, many=True)
        
        return Response({
            'bookings': serializer.data,
            'total_confirmed': Booking.objects.filter(user=request.user, status='confirmed').count(),
            'total_completed': Booking.objects.filter(user=request.user, status='completed').count(),
            'total_cancelled': Booking.objects.filter(user=request.user, status='cancelled').count()
        })

    @action(detail=True, methods=['post'])
    def student_cancel(self, request, pk=None):
        """
        Cancel a booking from the user side
        """
        booking = self.get_object()
        
        # Verify the booking belongs to the requesting user
        if booking.user != request.user:
            raise PermissionDenied("This booking doesn't belong to you")

        # Can only cancel confirmed bookings
        if booking.status != 'confirmed':
            return Response({
                'error': 'Can only cancel confirmed bookings'
            }, status=400)

        # Check cancellation policy
        class_option = booking.schedule_instance.schedule.option
        cancellation_policy = class_option.cancellationPolicy
        
        current_time = timezone.now()
        class_time = datetime.combine(
            booking.schedule_instance.date,
            booking.schedule_instance.time
        )
        class_time = timezone.make_aware(class_time)
        
        hours_until_class = (class_time - current_time).total_seconds() / 3600

        policy_hours = {
            '24h': 24,
            '48h': 48,
            '72h': 72,
            'flexible': float('inf')
        }

        if hours_until_class < policy_hours.get(cancellation_policy, 0):
            return Response({
                'error': f'Cancellation not allowed within {cancellation_policy} of class start'
            }, status=400)

        # Process cancellation
        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        booking.cancellation_reason = request.data.get('reason', '')
        booking.save()
        
        return Response(BookingDetailSerializer(booking).data)
