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
    ClassesMain,
    Reviews,
    Schedule,
    ScheduleInstance,
    Student
)
from ..serializers import (
    BookingCreateSerializer,
    BookingDetailSerializer,
    BookingListSerializer
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
        # Base queryset with all necessary joins
        queryset = Booking.objects.select_related(
            'schedule_instance__schedule__option__classId__businessId',
            'student__user'
        ).prefetch_related(
            models.Prefetch(
                'student__bookings',
                queryset=Booking.objects.filter(
                    status__in=['completed', 'cancelled']
                ).select_related('schedule_instance'),
                to_attr='group_bookings'
            )
        )

        # Apply filters
        filters = {}
        
        # Handle status filter
        if self.request.query_params.get('status'):
            status_values = self.request.query_params['status'].split(',')
            queryset = queryset.filter(status__in=status_values)
        else:
            # Default to confirmed bookings if no status specified
            queryset = queryset.filter(status='confirmed')
        
        # Apply other filters
        if self.request.query_params.get('student_email'):
            filters['student__user__email'] = self.request.query_params['student_email']
        if self.request.query_params.get('class_name'):
            filters['schedule_instance__schedule__option__classId__title__icontains'] = self.request.query_params['class_name']
                
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date and end_date:
            filters['schedule_instance__date__range'] = [start_date, end_date]

        # Apply search if provided
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(student__user__email__icontains=search) |
                Q(schedule_instance__schedule__option__classId__title__icontains=search)
            )

        # Apply ordering
        ordering = self.request.query_params.get('ordering')
        if ordering:
            order_fields = {
                'date': 'schedule_instance__date',
                '-date': '-schedule_instance__date',
                'student_name': 'student__user__first_name',
                '-student_name': '-student__user__first_name',
                'class_name': 'schedule_instance__schedule__option__classId__title',
                '-class_name': '-schedule_instance__schedule__option__classId__title'
            }
            
            if ordering in order_fields:
                queryset = queryset.order_by(order_fields[ordering])

        # Apply remaining filters
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
                    'student'
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

            # Student retention analysis
            student_bookings = (
                bookings
                .values('student')
                .annotate(booking_count=Count('id'))
            )
            
            total_students = student_bookings.count()
            repeat_students = student_bookings.filter(booking_count__gt=1).count()
            
            retention_rate = (
                (repeat_students / total_students) * 100
                if total_students > 0 else 0
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
                    unique_students=Count('student', distinct=True),
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
                    'unique_students': entry['unique_students'],
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
                    'student_retention_rate': round(retention_rate, 1)
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
    
    ################################################################################################

    @action(detail=False, methods=['get'])
    def my_bookings(self, request):
        """
        Get the current user's bookings with optional status filtering
        """
        try:
            student = Student.objects.get(user=request.user)
            status = request.query_params.get('status', 'confirmed')
            
            queryset = Booking.objects.select_related(
                'schedule_instance__schedule__option__classId__businessId',
                'student__user'
            ).prefetch_related(
                'schedule_instance__schedule__option__classId__images'
            ).filter(
                student=student,
                status=status
            ).order_by('schedule_instance__date', 'schedule_instance__time')

            bookings = []
            for booking in queryset:
                class_info = booking.schedule_instance.schedule.option.classId
                class_images = list(class_info.images.all())
                
                bookings.append({
                    'id': booking.id,
                    'booking_group_id': booking.booking_group_id,
                    'class_name': class_info.title,
                    'business_name': class_info.businessId.businessName,
                    'date': booking.schedule_instance.date,
                    'time': booking.schedule_instance.time,
                    'status': booking.status,
                    'amount_paid': float(booking.amount_paid),
                    'participants': booking.participants,
                    'coordinates': class_info.coordinates,  # Send the coordinates
                    'notes': booking.notes,
                    'attendance_marked': booking.attendance_marked,
                    'attended': booking.attended,
                    'enrollment_type': booking.enrollment_type,
                    'images': [
                        {'id': img.imageId, 'url': img.image.url}
                        for img in class_images
                    ] if class_images else []
                })

            return Response({
                'bookings': bookings,
                'total_confirmed': Booking.objects.filter(student=student, status='confirmed').count(),
                'total_completed': Booking.objects.filter(student=student, status='completed').count(),
                'total_cancelled': Booking.objects.filter(student=student, status='cancelled').count()
            })
            
        except Student.DoesNotExist:
            return Response({
                'error': 'No student profile found'
            }, status=400)
        except Exception as e:
            logger.error(f"Error fetching student bookings: {str(e)}")
            return Response({
                'error': 'Failed to fetch bookings'
            }, status=500)

    @action(detail=True, methods=['post'])
    def student_cancel(self, request, pk=None):
        """
        Cancel a booking from the student side
        """
        booking = self.get_object()
        
        # Verify the booking belongs to the requesting student
        student = Student.objects.get(user=request.user)
        if booking.student != student:
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

    @action(detail=True, methods=['post'])
    def student_reschedule(self, request, pk=None):
        """
        Reschedule a booking from the student side
        """
        booking = self.get_object()
        
        # Verify ownership
        student = Student.objects.get(user=request.user)
        if booking.student != student:
            raise PermissionDenied("This booking doesn't belong to you")

        # Validate new schedule instance
        try:
            new_instance = ScheduleInstance.objects.get(
                id=request.data.get('schedule_instance_id')
            )
        except ScheduleInstance.DoesNotExist:
            return Response({
                'error': 'Invalid schedule instance'
            }, status=400)

        # Check if instance can accommodate the booking
        if not new_instance.can_accommodate(booking.participants):
            return Response({
                'error': 'Selected time slot is full'
            }, status=400)

        # Update the booking
        booking.schedule_instance = new_instance
        booking.save()
        
        return Response(BookingDetailSerializer(booking).data)