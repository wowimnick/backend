# Django imports
import uuid
from django.db import models, transaction
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

from quickstart.constants import BookingChoices

# Local imports
from ..utils.permissions import check_user_role, IsBusinessOwner, IsManager
from ..models import (
    BusinessInfo,
    Booking,
    ClassesMain,
    Reviews,
    Schedule,
    ScheduleInstance,
    Student
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

logger = logging.getLogger(__name__)
from datetime import timedelta

class BookingViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        user = self.request.user
        status_filter = self.request.query_params.get('status')
        
        queryset = Booking.objects.select_related(
            'schedule_instance__schedule__option__classId__businessId',
            'student__user'
        )
        
        if status_filter:
            status_values = [s.strip() for s in status_filter.split(',')]
            queryset = queryset.filter(status__in=status_values)
            
        # For business owners/managers - show all bookings for their business
        if check_user_role(user, ['Business Owner', 'Manager']):
            return queryset.filter(
                schedule_instance__schedule__option__classId__businessId__in=[
                    b.businessId for b in user.owned_businesses.all()
                ] +
                [b.businessId for b in user.managed_businesses.all()]
            ).distinct()
                
        # For students - show only their bookings
        return queryset.filter(student__user=user)

    def get_serializer_class(self):
        if self.action == 'create':
            return BookingCreateSerializer
        return BookingDetailSerializer

    @action(detail=False, methods=['post'])
    def check_availability(self, request):
        """
        Check availability for recurring bookings before attempting to book
        """
        serializer = BookingCreateSerializer(data=request.data)
        try:
            serializer.validate_schedule_instances(request.data.get('schedule_instances', []))
            return Response({
                'available': True,
                'message': 'All slots are available'
            })
        except ValidationError as e:
            if 'unavailable_slots' in getattr(e.detail, 'keys', lambda: {})():
                return Response({
                    'available': False,
                    'unavailable_slots': e.detail['unavailable_slots'],
                    'message': e.detail['message']
                }, status=status.HTTP_200_OK)
            return Response({
                'available': False,
                'error': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                logger.info("Starting booking creation process")
                logger.info(f"Request data: {request.data}")
                
                serializer = BookingCreateSerializer(data=request.data)
                if not serializer.is_valid():
                    logger.error(f"Serializer errors: {serializer.errors}")
                    return Response(
                        serializer.errors,
                        status=status.HTTP_400_BAD_REQUEST
                    )

                validated_data = serializer.validated_data
                schedule_instances = validated_data['schedule_instances']
                booking_type = validated_data['booking_type']
                recurrence_pattern = validated_data['recurrence_pattern']

                # Add debug logging for booking type
                logger.info(f"Booking type: {booking_type}")
                logger.info(f"Recurrence pattern: {recurrence_pattern}")
                logger.info(f"Initial schedule instances: {schedule_instances}")

                # Generate booking group ID
                booking_group_id = uuid.uuid4()
                logger.info(f"Generated booking group ID: {booking_group_id}")
                
                # Get or create student profile
                student = self._get_or_create_student(request.user)
                logger.info(f"Student: {student.id}")
                
                bookings = []
                total_price = 0
                
                # For recurring bookings, create future bookings
                if booking_type == 'Recurring Classes':  # This is the exact string we should match
                    logger.info("Processing recurring booking")
                    weeks = 8 if recurrence_pattern == 'biweekly' else 4
                    logger.info(f"Total weeks to book: {weeks}")
                    
                    for instance_id in schedule_instances:
                        instance = ScheduleInstance.objects.get(id=instance_id)
                        schedule = instance.schedule
                        price_per_session = instance.price
                        total_sessions = weeks // 2 if recurrence_pattern == 'biweekly' else weeks

                        logger.info(f"""
                        Processing instance:
                        - Instance ID: {instance_id}
                        - Schedule ID: {schedule.id}
                        - Date: {instance.date}
                        - Time: {instance.time}
                        - Price per session: {price_per_session}
                        - Total sessions: {total_sessions}
                        """)

                        # Calculate total price for all sessions
                        session_total_price = price_per_session * total_sessions
                        total_price += session_total_price

                        # Create initial booking
                        current_date = instance.date
                        initial_booking = self._create_booking(
                            instance, 
                            student, 
                            booking_group_id,
                            price_per_session
                        )
                        bookings.append(initial_booking)
                        logger.info(f"Created initial booking for date: {current_date}")

                        # Create future bookings
                        for week in range(1, weeks):
                            logger.info(f"Processing week {week}")
                            
                            if recurrence_pattern == 'biweekly' and week % 2 == 1:
                                logger.info(f"Skipping week {week} (biweekly pattern)")
                                continue
                                
                            future_date = current_date + timedelta(weeks=week)
                            logger.info(f"Creating booking for date: {future_date}")
                            
                            try:
                                future_instance = ScheduleInstance.objects.get(
                                    schedule=schedule,
                                    date=future_date,
                                    time=instance.time
                                )
                            except ScheduleInstance.DoesNotExist:
                                future_instance = ScheduleInstance.objects.create(
                                    schedule=schedule,
                                    date=future_date,
                                    time=instance.time,
                                    price=instance.price,
                                    max_participants=instance.max_participants,
                                    status='scheduled'
                                )
                            
                            future_booking = self._create_booking(
                                future_instance,
                                student,
                                booking_group_id,
                                price_per_session
                            )
                            bookings.append(future_booking)
                            logger.info(f"Created future booking for date: {future_date}")
                else:
                    logger.info("Processing single booking")
                    bookings.extend(
                        self._create_single_bookings(
                            schedule_instances,
                            student,
                            booking_group_id
                        )
                    )
                    total_price = sum(b.amount_paid for b in bookings)

                logger.info(f"""
                Booking creation completed:
                - Total bookings created: {len(bookings)}
                - Total price: {total_price}
                - First booking date: {bookings[0].schedule_instance.date if bookings else None}
                - Last booking date: {bookings[-1].schedule_instance.date if bookings else None}
                """)

                return Response({
                    'success': True,
                    'data': {
                        'booking_group_id': booking_group_id,
                        'bookings': BookingDetailSerializer(bookings, many=True).data,
                        'total_price': total_price
                    }
                }, status=status.HTTP_201_CREATED)
                
        except Exception as e:
            logger.exception("Error in booking creation")
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    def _get_or_create_future_instance(self, schedule, date, time, price, max_participants):
        """Get an existing instance or create a new one for the given date"""
        try:
            return ScheduleInstance.objects.get(
                schedule=schedule,
                date=date,
                time=time
            )
        except ScheduleInstance.DoesNotExist:
            logger.info(f"Creating new instance for date: {date}")
            return ScheduleInstance.objects.create(
                schedule=schedule,
                date=date,
                time=time,
                price=price,
                max_participants=max_participants,
                status='scheduled'
            )

    def _create_booking(self, instance, student, booking_group_id, amount_paid=None):
        """Create individual booking with optional amount override"""
        option = instance.schedule.option
        booking_type = option.booking_type
        
        logger.info(f"""
        Creating booking:
        - Instance ID: {instance.id}
        - Date: {instance.date}
        - Time: {instance.time}
        - Amount: {amount_paid if amount_paid is not None else instance.price}
        - Booking Type: {booking_type}
        """)
        
        booking = Booking.objects.create(
            schedule_instance=instance,
            student=student,
            booking_group_id=booking_group_id,
            participants=1,
            amount_paid=amount_paid if amount_paid is not None else instance.price,
            status='confirmed',
            payment_status='paid',
            enrollment_type=booking_type,
            sessions_per_week=(
                option.sessions_per_week 
                if booking_type == 'Recurring Classes'
                else None
            ),
            recurrence_pattern=(
                option.recurrence_pattern 
                if booking_type == 'Recurring Classes'
                else None
            )
        )
        
        logger.info(f"Created booking with ID: {booking.id}")
        return booking

    def _get_or_create_student(self, user):
        """
        Get or create a student profile for the user
        """
        student, _ = Student.objects.get_or_create(
            user=user,
            defaults={'enrollment_date': timezone.now().date()}
        )
        return student

    def _create_recurring_bookings(self, initial_instances, student, booking_group_id, pattern):
        """Create bookings for all recurring instances"""
        weeks = 8 if pattern == 'biweekly' else 4
        bookings = []

        for instance_id in initial_instances:
            instance = ScheduleInstance.objects.get(id=instance_id)
            current_date = instance.date
            schedule = instance.schedule

            # Create initial booking
            bookings.append(self._create_booking(instance, student, booking_group_id))

            # Create future bookings
            for week in range(1, weeks + 1):
                if pattern == 'biweekly' and week % 2 == 1:
                    continue
                    
                future_date = current_date + timedelta(weeks=week)
                future_instance = ScheduleInstance.objects.get(
                    schedule=schedule,
                    date=future_date,
                    time=instance.time
                )
                
                bookings.append(self._create_booking(future_instance, student, booking_group_id))

        return bookings

    def _create_single_bookings(self, instances, student, booking_group_id):
        """Create bookings for single session instances"""
        return [
            self._create_booking(
                ScheduleInstance.objects.select_related('schedule__option').get(id=instance_id),
                student,
                booking_group_id
            )
            for instance_id in instances
        ]

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        
        # Can only cancel confirmed bookings
        if booking.status != 'confirmed':
            return Response(
                {'error': 'Can only cancel confirmed bookings'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # For related bookings, allow cancelling all future sessions
        cancel_all = request.data.get('cancel_all', False)
        if cancel_all and booking.booking_group_id:
            future_bookings = Booking.objects.filter(
                booking_group_id=booking.booking_group_id,
                schedule_instance__date__gte=timezone.now().date(),
                status='confirmed'
            )
            
            for b in future_bookings:
                b.status = 'cancelled'
                b.cancelled_at = timezone.now()
                b.cancellation_reason = request.data.get('reason', '')
                b.save()
                
            return Response({
                'message': 'All future bookings cancelled',
                'cancelled_count': future_bookings.count()
            })
            
        # Regular single booking cancellation
        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        booking.cancellation_reason = request.data.get('reason', '')
        booking.save()
        
        return Response(BookingDetailSerializer(booking).data)

    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """Get analytics for bookings"""
        try:
            # Get business and date range
            business = self._get_business(request.user)
            start_date, end_date = self._get_date_range(request)
            
            # Get base queryset
            bookings = self._get_analytics_queryset(business, start_date, end_date)
            
            # Calculate summary metrics
            daily_counts = bookings.annotate(
                date=TruncDate('booking_date')
            ).values('date').annotate(
                count=Count('id')
            )
            
            total_bookings = bookings.count()
            avg_daily = daily_counts.aggregate(avg=Avg('count'))['avg'] or 0
            
            # Get most popular day
            weekday_counts = bookings.annotate(
                weekday=ExtractWeekDay('booking_date')
            ).values('weekday').annotate(
                count=Count('id')
            ).order_by('-count')
            
            weekday_map = {
                1: 'Sunday', 2: 'Monday', 3: 'Tuesday', 4: 'Wednesday',
                5: 'Thursday', 6: 'Friday', 7: 'Saturday'
            }
            
            most_popular_day = weekday_map[weekday_counts[0]['weekday']] if weekday_counts else 'N/A'
            
            # Get trends data
            trends = bookings.annotate(
                date=TruncDate('booking_date')
            ).values('date').annotate(
                bookings=Count('id')
            ).order_by('date')

            # Get bookings by day
            by_day = bookings.annotate(
                day=ExtractWeekDay('booking_date')
            ).values('day').annotate(
                bookings=Count('id')
            ).order_by('day')
            
            by_day_formatted = [
                {
                    'day': weekday_map[day['day']],
                    'bookings': day['bookings']
                }
                for day in by_day
            ]

            # Get bookings by type
            by_type = bookings.values(
                type=F('schedule_instance__schedule__option__classId__category')
            ).annotate(
                bookings=Count('id')
            ).order_by('-bookings')
            
            response_data = {
                'summary': {
                    'total_bookings': total_bookings,
                    'average_daily': round(avg_daily, 1),
                    'most_popular_day': most_popular_day
                },
                'trends': [
                    {
                        'date': entry['date'].isoformat(),
                        'bookings': entry['bookings']
                    }
                    for entry in trends
                ],
                'by_day': by_day_formatted,
                'by_type': list(by_type)
            }
            
            return Response(response_data)
            
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

    @action(detail=True, methods=['post'])
    def mark_attendance(self, request, pk=None):
        booking = self.get_object()
        
        if booking.status != 'confirmed':
            return Response(
                {'error': 'Can only mark attendance for confirmed bookings'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        booking.attendance_marked = True
        booking.attended = request.data.get('attended', False)
        booking.save()
        
        return Response(BookingDetailSerializer(booking).data)