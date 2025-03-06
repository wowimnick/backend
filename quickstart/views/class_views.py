from datetime import datetime, time
from silk.profiling.profiler import silk_profile
import json
from django.forms import ValidationError
from rest_framework import generics, viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.permissions import BasePermission, IsAuthenticated, AllowAny
from rest_framework.request import Request
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Case, Sum, When, IntegerField, Q, Subquery, Count, F, Avg
from django.db.models.functions import Coalesce
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import api_view, permission_classes
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

import logging

from ..models import (
    Booking, BusinessInfo, ClassesMain, ClassImage, Reviews, ClassOption, Schedule, ScheduleBreak, ScheduleInstance
)
from ..serializers import (
    ClassesMainSerializer,
    ClassImageSerializer,
    ClassOptionSerializer,
    ClassCreateSerializer,
    ScheduleSerializer,
    ClassOptionCreateSerializer,
    ScheduleInstanceSerializer,
    ScheduleBreakSerializer
)

from ..utils.permissions import check_user_role, BaseUserDataPermission
from .utils import haversine_distance
from quickstart import models

logger = logging.getLogger(__name__)
    
def validate_schedule_conflicts(option, schedule_data, exclude_id=None):
    """
    Validate schedule for time conflicts within an option.
    
    Args:
        option: ClassOption instance
        schedule_data: Dict containing time (string or time object), and duration (int)
                      For courses: also contains 'day' string
                      For single sessions: contains 'date' string
        exclude_id: Optional ID to exclude from conflict check (for updates)
    
    Returns:
        (bool, str): Tuple of (is_valid, error_message)
    """
    # Convert schedule time to minutes since midnight for comparison
    def time_to_minutes(time_val):
        if isinstance(time_val, str):
            time_obj = datetime.strptime(time_val, '%H:%M').time()
        elif isinstance(time_val, time):
            time_obj = time_val
        else:
            raise ValueError(f"Invalid time format: {time_val}")
        return time_obj.hour * 60 + time_obj.minute

    # Get time range for the proposed schedule
    try:
        proposed_start = time_to_minutes(schedule_data['time'])
        duration = int(schedule_data.get('duration', 60))
        proposed_end = proposed_start + duration

        # Check against existing schedules based on booking type
        existing_schedules = option.schedules.all()
        if exclude_id:
            existing_schedules = existing_schedules.exclude(id=exclude_id)

        if option.booking_type == 'Full Course':
            if 'day' not in schedule_data:
                return False, "Day is required for course schedules"
            proposed_day = schedule_data['day']
            
            # Check conflicts with other course schedules on the same day
            for existing in existing_schedules:
                if existing.day == proposed_day:
                    existing_start = time_to_minutes(existing.time)
                    existing_end = existing_start + existing.duration
                    if (proposed_start < existing_end and proposed_end > existing_start):
                        return False, f"Schedule conflicts with existing class at {existing.time} on {existing.day}"
        else:
            # For single sessions, check date conflicts
            if 'date' not in schedule_data:
                return False, "Date is required for single sessions"
            proposed_date = schedule_data['date']
            
            # Check conflicts with other sessions on the same date
            for existing in existing_schedules:
                if existing.date == proposed_date:
                    existing_start = time_to_minutes(existing.time)
                    existing_end = existing_start + existing.duration
                    if (proposed_start < existing_end and proposed_end > existing_start):
                        return False, f"Schedule conflicts with existing session at {existing.time} on {existing.date}"

        return True, None
        
    except (ValueError, KeyError) as e:
        return False, f"Invalid schedule data: {str(e)}"

class BusinessPermissionMixin:
    """Mixin to handle business-specific permissions"""
    request: Request

    @silk_profile(name='BusinessPermissionMixin_get_queryset')
    def get_business_queryset(self, queryset):
        """
        Filter queryset based on user type:
        - Public users/students see classes with at least one active option
        - Business owners/managers see their own classes
        - Admins see everything
        """
        user = self.request.user
        
        # For unauthenticated users or regular students, show only classes with active options
        if not user.is_authenticated or (user.role and user.role.name == 'Student'):
            return queryset.filter(
                Exists(
                    ClassOption.objects.filter(
                        classId=OuterRef('pk'),
                        active=True
                    )
                )
            )
        
        # Admins can see everything
        if check_user_role(user, ['Admin', 'Super Admin']):
            return queryset
            
        # Business owners/managers see their classes
        return queryset.filter(
            Q(businessId__owner=user) |
            Q(businessId__managers=user)
        ).distinct()

    @silk_profile(name='BusinessPermissionMixin_check_business_permission')
    def check_business_permission(self, business_id):
        """Check if user has permission to modify business data"""
        user = self.request.user
        
        if check_user_role(user, ['Admin', 'Super Admin']):
            return True
            
        return BusinessInfo.objects.filter(
            businessId=business_id
        ).filter(
            Q(owner=user) |
            Q(managers=user)
        ).exists()
    
class BusinessClassesPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        """
        Global permissions check for accessing class views.
        """
        # Public read access is always allowed
        if request.method in permissions.SAFE_METHODS and not getattr(view, 'is_business_context', False):
            return True

        # For business or write operations, require authentication
        if not request.user.is_authenticated:
            return False

        # Admin/Super Admin can do anything
        if check_user_role(request.user, ['Admin', 'Super Admin']):
            return True

        # For business context or write operations, check business roles
        if getattr(view, 'is_business_context', False) or request.method not in permissions.SAFE_METHODS:
            return check_user_role(request.user, ['Business Owner', 'Manager'])

        return True
    
    def has_object_permission(self, request, view, obj):
        """
        Object-level permission check for specific class instances.
        """
        # Public read access is always allowed
        if request.method in permissions.SAFE_METHODS and not getattr(view, 'is_business_context', False):
            return True

        # Admin/Super Admin can do anything
        if check_user_role(request.user, ['Admin', 'Super Admin']):
            return True

        # Check if user owns or manages the business
        try:
            is_owner = obj.businessId.owner == request.user
            is_manager = obj.businessId.managers.filter(id=request.user.id).exists()
            return is_owner or is_manager
        except AttributeError:
            return False
    

class ClassViewSet(viewsets.ModelViewSet):
    serializer_class = ClassesMainSerializer
    permission_classes = [BusinessClassesPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_business_context = False

    def get_serializer_class(self):
        if self.action == 'create':
            return ClassCreateSerializer
        return ClassesMainSerializer
    
    @silk_profile(name='ClassViewSet_get_queryset')
    def get_queryset(self):
        # Base queryset
        queryset = ClassesMain.objects.all()

        # Calculate confirmed bookings per schedule instance
        confirmed_bookings_subquery = Subquery(
            Booking.objects.filter(
                schedule_instance_id=OuterRef('id'),
                status='confirmed'
            )
            .values('schedule_instance_id')
            .annotate(total=Count('id'))
            .values('total')
        )

        # Calculate total users per option
        user_count_subquery = Subquery(
            Booking.objects.filter(
                schedule_instance__schedule__option=OuterRef('pk'),
                status='confirmed'
            )
            .values('schedule_instance__schedule__option')
            .annotate(count=Count('user', distinct=True))
            .values('count')[:1]
        )

        # Calculate average rating for each class
        average_rating_subquery = Subquery(
            Reviews.objects.filter(
                classId=OuterRef('pk')
            )
            .values('classId')
            .annotate(avg_rating=Avg('rating'))
            .values('avg_rating')[:1]
        )

        # Calculate total reviews for each class
        review_count_subquery = Subquery(
            Reviews.objects.filter(
                classId=OuterRef('pk')
            )
            .values('classId')
            .annotate(count=Count('reviewId'))
            .values('count')[:1]
        )

        # Add all necessary prefetches and annotations
        queryset = queryset.select_related('businessId').prefetch_related(
            # Prefetch options with annotations
            Prefetch(
                'options',
                queryset=ClassOption.objects.annotate(
                    total_students=Coalesce(user_count_subquery, 0)
                ).prefetch_related(
                    # Prefetch schedules
                    Prefetch(
                        'schedules',
                        queryset=Schedule.objects.filter(
                            is_active=True
                        ).select_related('option')
                    ),
                    # Prefetch schedule instances with booking counts
                    Prefetch(
                        'schedules__instances',
                        queryset=ScheduleInstance.objects.annotate(
                            confirmed_bookings=Coalesce(confirmed_bookings_subquery, 0)
                        ).filter(
                            date__gte=timezone.now().date(),
                            status='scheduled'
                        )
                    ),
                    # Prefetch schedule breaks
                    'schedules__breaks'
                )
            ),
            # Prefetch images
            'images'
        ).annotate(
            # Add class-level annotations
            total_students=Count(
                'options__schedules__instances__bookings__user',
                filter=Q(
                    options__schedules__instances__bookings__status='confirmed'
                ),
                distinct=True
            ),
            total_bookings=Count(
                'options__schedules__instances__bookings',
                filter=Q(
                    options__schedules__instances__bookings__status='confirmed'
                )
            ),
            active_options_count=Count(
                'options',
            ),
            # Add review statistics
            average_rating=Coalesce(average_rating_subquery, 0.0),
            review_count=Coalesce(review_count_subquery, 0),
            # Add business name annotation
            business_name=F('businessId__businessName')
        )

        if self.is_business_context:
            if check_user_role(self.request.user, ['Admin', 'Super Admin']):
                return queryset

            return queryset.filter(
                Q(businessId__owner=self.request.user) |
                Q(businessId__managers=self.request.user)
            ).distinct()

        # Public context - show only active classes directly
        return queryset.filter(active=True)
        
    @silk_profile(name='ClassViewSet_search')
    @action(detail=False, methods=['get'])
    def search(self, request):
        """
        Optimized search endpoint that returns filtered search results
        """
        try:
            # Get search parameters
            lat = request.query_params.get('lat')
            lng = request.query_params.get('lng')
            radius = float(request.query_params.get('radius', 100))
            location = request.query_params.get('location')
            
            # Get filter parameters
            price_min = request.query_params.get('price_min')
            price_max = request.query_params.get('price_max')
            distance_max = request.query_params.get('distance_max')
            time_preferences = request.query_params.getlist('time_preference')
            days = request.query_params.getlist('days')
            class_type = request.query_params.get('class_type')
            
            # Start with base queryset
            queryset = self.get_queryset()

            # Apply location filters if provided
            if location:
                location_filter = Q()
                location_terms = location.split()
                
                for term in location_terms:
                    location_filter |= (
                        Q(location__icontains=term) |
                        Q(businessId__businessCity__icontains=term) |
                        Q(businessId__businessState__icontains=term)
                    )
                queryset = queryset.filter(location_filter)

            # Apply price filter
            if price_max:
                queryset = queryset.filter(
                    options__schedules__price__lte=price_max
                )

            # Apply distance filter if coordinates provided
            if lat and lng and distance_max:
                # Implementation depends on your distance calculation method
                pass

            # Apply time preference filter
            if time_preferences:
                time_ranges = {
                    'Morning (6am-12pm)': (time(6, 0), time(12, 0)),
                    'Afternoon (12pm-5pm)': (time(12, 0), time(17, 0)),
                    'Evening (5pm-10pm)': (time(17, 0), time(22, 0))
                }
                
                time_filter = Q()
                for pref in time_preferences:
                    if pref in time_ranges:
                        start, end = time_ranges[pref]
                        time_filter |= Q(
                            options__schedules__time__gte=start,
                            options__schedules__time__lte=end
                        )
                
                if time_filter:
                    queryset = queryset.filter(time_filter)

            # Apply days filter
            if days:
                day_map = {
                    'Monday': 'Mon',
                    'Tuesday': 'Tue',
                    'Wednesday': 'Wed',
                    'Thursday': 'Thu',
                    'Friday': 'Fri',
                    'Saturday': 'Sat',
                    'Sunday': 'Sun'
                }
                short_days = [day_map[day] for day in days if day in day_map]
                if short_days:
                    queryset = queryset.filter(options__schedules__day__in=short_days)

            # Apply class type filter
            if class_type:
                if class_type == 'course':
                    queryset = queryset.filter(options__booking_type='Full Course')
                else:
                    queryset = queryset.filter(options__booking_type='Single Session')

            # Ensure distinct results
            queryset = queryset.distinct()

            serializer = ClassesMainSerializer(queryset, many=True)
            return Response({
                'results': serializer.data
            })
                
        except Exception as e:
            logger.error(f"Search error: {str(e)}", exc_info=True)
            return Response({"error": str(e)}, status=500)
        
    @action(detail=False, methods=['get'])
    def business_classes(self, request):
        """Endpoint for business dashboard"""
        self.is_business_context = True
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def business_detail(self, request, pk=None):
        """Detailed view for business dashboard"""
        self.is_business_context = True
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @silk_profile(name='ClassViewSet_images')
    @action(detail=True, methods=['get', 'post'])
    def images(self, request, pk=None):
        """Handle class images list and creation"""
        if request.method == 'GET':
            images = ClassImage.objects.filter(classId=pk)
            serializer = ClassImageSerializer(images, many=True)
            return Response(serializer.data)
        
        elif request.method == 'POST':
            self.is_business_context = True
            class_instance = self.get_object()
            
            # Handle multiple images
            images = request.FILES.getlist('images')
            created_images = []
            
            for image in images:
                img = ClassImage.objects.create(
                    classId=class_instance,
                    image=image
                )
                created_images.append(img)
            
            serializer = ClassImageSerializer(created_images, many=True)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['delete'], url_path='images/(?P<image_id>[^/.]+)')
    def delete_image(self, request, pk=None, image_id=None):
        """Delete specific class image"""
        self.is_business_context = True
        class_instance = self.get_object()
        
        try:
            image = ClassImage.objects.get(
                imageId=image_id,
                classId=class_instance
            )
            # Delete actual image file
            if image.image:
                image.image.delete(save=False)
            image.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
            
        except ClassImage.DoesNotExist:
            return Response(
                {'error': 'Image not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
    @action(detail=True, methods=['post'])
    def toggle_class_active(self, request, pk=None):
        """Toggle class active status directly"""
        self.is_business_context = True
        class_instance = self.get_object()
        
        # Get active status from request or toggle current value
        active = request.data.get('active')
        if active is None:
            active = not class_instance.active
        
        class_instance.active = active
        class_instance.save()
        
        return Response({
            'active': class_instance.active,
            'classId': class_instance.classId
        })
    
    def create(self, request, *args, **kwargs):
        self.is_business_context = True
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        self.is_business_context = True
        return super().update(request, *args, **kwargs)
    
    def perform_update(self, serializer):
        instance = serializer.save()
        
        # Handle image uploads if present
        if 'classImages' in self.request.FILES:
            images = self.request.FILES.getlist('classImages')
            for image in images:
                ClassImage.objects.create(classId=instance, image=image)

    def destroy(self, request, *args, **kwargs):
        """
        Override destroy method to deactivate schedules instead of deleting them.
        """
        instance = self.get_object()
        
        # Check if force_delete parameter is passed (for admin use only)
        force_delete = request.query_params.get('force_delete', 'false').lower() == 'true'
        
        # Only allow force delete for admin users
        if force_delete and not check_user_role(request.user, ['Admin', 'Super Admin']):
            force_delete = False
        
        try:
            # Use our custom delete method which deactivates instead of deleting
            instance.delete(force_delete=force_delete)
            return Response(status=status.HTTP_204_NO_CONTENT)
            
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class ClassImageList(generics.ListCreateAPIView):
    serializer_class = ClassImageSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        return ClassImage.objects.filter(classId=self.kwargs['pk'])
    
    def perform_create(self, serializer):
        class_instance = ClassesMain.objects.get(pk=self.kwargs['pk'])
        serializer.save(classId=class_instance)

class ClassImageDetail(BusinessPermissionMixin, generics.RetrieveDestroyAPIView):
    queryset = ClassImage.objects.all()
    serializer_class = ClassImageSerializer

    @silk_profile(name='ClassImageDetail_get_queryset')
    def get_queryset(self):
        queryset = super().get_queryset()
        return self.get_business_queryset(queryset)
    
    @silk_profile(name='ClassImageDetail_get_object')
    def get_object(self):
        obj = super().get_object()
        if self.request.method == 'DELETE':
            if not self.check_business_permission(obj.classId.businessId.businessId):
                raise PermissionDenied("You don't have permission to delete this image")
        return obj


class ScheduleViewSet(BusinessPermissionMixin, viewsets.ModelViewSet):
    serializer_class = ScheduleSerializer
    
    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated()]
        return []
    
    @silk_profile(name='ScheduleViewSet_get_queryset')
    def get_queryset(self):
        queryset = Schedule.objects.all()
        
        # Filter by option if provided
        option_id = self.request.query_params.get('option_id')
        if option_id:
            queryset = queryset.filter(option_id=option_id)
            
        # Only show active schedules for non-staff users
        if not check_user_role(self.request.user, ['Admin', 'Business Owner', 'Manager']):
            queryset = queryset.filter(is_active=True)
            
        return queryset
    
    def create(self, request, *args, **kwargs):
        # Extract and validate option_id first
        option_id = request.data.get('option_id')
        if not option_id:
            return Response(
                {'error': 'option_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            option = ClassOption.objects.get(optionId=option_id)
        except ClassOption.DoesNotExist:
            return Response(
                {'error': 'Invalid option_id'},
                status=status.HTTP_404_NOT_FOUND
            )

        if not self.check_business_permission(option.classId.businessId.businessId):
            raise PermissionDenied("You don't have permission to create schedules for this class")

        # Validate conflicts using schedule data (which now includes duration)
        is_valid, error_message = validate_schedule_conflicts(option, request.data)
        if not is_valid:
            return Response(
                {'error': error_message},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Create the serializer with the validated option
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Perform the creation with the option
        serializer.save(option=option)
        
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        
        # Validate conflicts before updating
        is_valid, error_message = validate_schedule_conflicts(
            instance.option, 
            request.data,
            exclude_id=instance.id
        )
        if not is_valid:
            return Response(
                {'error': error_message},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def add_break(self, request, pk=None):
        """Add a break period to the schedule"""
        schedule = self.get_object()
        serializer = ScheduleBreakSerializer(data={
            'schedule': schedule.id,
            **request.data
        })
        
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['get'], url_path='availability')
    def availability(self, request):
        """Get availability for specific option's schedules within a date range"""
        option_id = request.query_params.get('option_id')
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        is_course = request.query_params.get('is_course', 'false').lower() == 'true'
        
        if not option_id:
            return Response(
                {'error': 'option_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Get the option to check booking type
            option = ClassOption.objects.get(optionId=option_id)
            
            # Convert date strings to date objects
            start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else timezone.now().date()
            end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else (start_date + timezone.timedelta(days=30 if is_course else 7))

            if is_course:
                # For courses, get unique schedules first
                schedules = Schedule.objects.filter(
                    option_id=option_id,
                    is_active=True,
                    start_date__gte=start_date,
                ).select_related('option')

                available_instances = []
                
                for schedule in schedules:
                    # Get first instance of each course schedule
                    first_instance = ScheduleInstance.objects.filter(
                        schedule=schedule,
                        status='scheduled'
                    ).annotate(
                        current_bookings_count=Coalesce(
                            Sum(Case(
                                When(bookings__status='confirmed', then='bookings__participants'),
                                default=0
                            )),
                            0,
                            output_field=IntegerField()
                        )
                    ).order_by('date').first()

                    if first_instance:
                        available_spots = first_instance.max_participants - first_instance.current_bookings_count
                        if available_spots > 0:
                            available_instances.append({
                                'id': first_instance.pk,
                                'date': first_instance.date,
                                'time': first_instance.time,
                                'start_date': schedule.start_date,
                                'duration': first_instance.duration,
                                'day': schedule.day,
                                'available_spots': available_spots,
                                'price': str(first_instance.price),
                                'end_date': schedule.end_date
                            })
            else:
                # Original single session logic
                instances = ScheduleInstance.objects.filter(
                    schedule__option_id=option_id,
                    schedule__is_active=True,
                    date__range=(start_date, end_date),
                    status='scheduled'
                ).annotate(
                    current_bookings_count=Coalesce(
                        Sum(Case(
                            When(bookings__status='confirmed', then='bookings__participants'),
                            default=0
                        )),
                        0,
                        output_field=IntegerField()
                    )
                ).select_related('schedule')

                available_instances = []
                for instance in instances:
                    available_spots = instance.max_participants - instance.current_bookings_count
                    if available_spots > 0:
                        available_instances.append({
                            'id': instance.pk,
                            'date': instance.date,
                            'time': instance.time,
                            'duration': instance.duration,
                            'day': instance.schedule.day,
                            'available_spots': available_spots,
                            'price': str(instance.price),
                            'end_date': None
                        })

            return Response({
                'schedules': available_instances,
                'total_available': len(available_instances),
                'date_range': {
                    'start_date': start_date,
                    'end_date': end_date
                }
            })

        except ClassOption.DoesNotExist:
            return Response(
                {'error': 'Invalid option ID'},
                status=status.HTTP_404_NOT_FOUND
            )
        except ValueError as e:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Error fetching availability: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @silk_profile()
    @action(detail=False, methods=['get'], url_path='available-days')
    def available_days(self, request):
        """Get available days for a month where schedules exist"""
        option_id = request.query_params.get('option_id')
        month = request.query_params.get('month')
        year = request.query_params.get('year')

        if not all([option_id, month, year]):
            return Response(
                {'error': 'option_id, month, and year are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Get all active schedules for the option
            schedules = Schedule.objects.filter(
                option_id=option_id,
                option__active=True,
                is_active=True
            )

            # Convert day names to numbers (assuming Schedule.day uses Mon, Tue, etc.)
            day_to_number = {
                'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 
                'Fri': 4, 'Sat': 5, 'Sun': 6
            }

            # Get all days of week that have schedules
            schedule_days = set([day_to_number[schedule.day] for schedule in schedules if schedule.day and schedule.day in day_to_number])

            # Get all dates for the given month
            import calendar
            cal = calendar.monthcalendar(int(year), int(month))
            
            available_days = []
            for week in cal:
                for day_num, month_day in enumerate(week):
                    if month_day != 0 and day_num in schedule_days:
                        available_days.append(month_day)

            return Response({
                'available_days': sorted(available_days),
                'month': month,
                'year': year
            })

        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        
class ScheduleInstanceViewSet(viewsets.ModelViewSet):
    permission_classes = [BaseUserDataPermission]
    serializer_class = ScheduleInstanceSerializer
    
    @silk_profile()
    def get_queryset(self):
        queryset = ScheduleInstance.objects.all()
        
        # Only include instances from active schedules by default
        include_inactive = self.request.query_params.get('include_inactive', 'false').lower() == 'true'
        if not include_inactive:
            queryset = queryset.filter(schedule__is_active=True)
        
        # Filter by schedule if provided
        schedule_id = self.request.query_params.get('schedule_id')
        if schedule_id:
            queryset = queryset.filter(schedule_id=schedule_id)
            
        # Filter by date range if provided
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date and end_date:
            queryset = queryset.filter(date__range=[start_date, end_date])
            
        # Only show scheduled/confirmed instances for non-staff users
        if not check_user_role(self.request.user, ['Admin', 'Business Owner', 'Manager']):
            queryset = queryset.filter(status='scheduled')
            
        return queryset

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a specific instance"""
        instance = self.get_object()
        reason = request.data.get('reason')
        
        if not reason:
            return Response(
                {'error': 'Cancellation reason is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        instance.status = 'cancelled'
        instance.cancellation_reason = reason
        instance.save()
        
        # Here you would handle any notification logic
        
        return Response(ScheduleInstanceSerializer(instance).data)

class ScheduleBreakViewSet(viewsets.ModelViewSet):
    permission_classes = [BaseUserDataPermission]
    serializer_class = ScheduleBreakSerializer
    
    def get_queryset(self):
        queryset = ScheduleBreak.objects.all()
        
        schedule_id = self.request.query_params.get('schedule_id')
        if schedule_id:
            queryset = queryset.filter(schedule_id=schedule_id)
            
        return queryset

    def perform_create(self, serializer):
        with transaction.atomic():
            break_period = serializer.save()
            
            # Cancel affected instances
            instances = ScheduleInstance.objects.filter(
                schedule=break_period.schedule,
                date__range=[break_period.start_date, break_period.end_date],
                status='scheduled'
            )
            
            instances.update(
                status='cancelled',
                cancellation_reason=f"Break period: {break_period.reason}"
            )
    
class ClassOptionDetail(BusinessPermissionMixin, generics.RetrieveUpdateDestroyAPIView):
    permission_classes = []  # Allow public access for GET
    serializer_class = ClassOptionCreateSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    
    @silk_profile()
    def get_queryset(self):
        # Match the URL parameter name
        return ClassOption.objects.filter(
            optionId=self.kwargs['option_id'],
            classId_id=self.kwargs['pk']
        )
    
    @silk_profile()
    def get_object(self):
        obj = get_object_or_404(
            ClassOption,
            optionId=self.kwargs['option_id'],
            classId_id=self.kwargs['pk']
        )
        self.check_object_permissions(self.request, obj)
        return obj

    def get_permissions(self):
        if self.request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            return [IsAuthenticated()]
        return []

    def check_object_permissions(self, request, obj):
        if request.method in ['PUT', 'PATCH', 'DELETE']:
            if not self.check_business_permission(obj.classId.businessId.businessId):
                raise PermissionDenied("You don't have permission to modify this class option")

    def post(self, request, *args, **kwargs):
        """Create new class - requires business owner/manager permission"""
        if not check_user_role(request.user, ['Business Owner', 'Manager', 'Admin', 'Super Admin']):
            raise PermissionDenied("You don't have permission to create classes")
                
        serializer = ClassCreateSerializer(
            data=request.data,
            context={'request': request}  # Add this line
        )
        serializer.is_valid(raise_exception=True)
        
        user = request.user
        try:
            # Get the user's business
            business = BusinessInfo.objects.get(owner=user)
        except BusinessInfo.DoesNotExist:
            try:
                # Check if they're a manager instead
                business = BusinessInfo.objects.get(managers=user)
            except BusinessInfo.DoesNotExist:
                raise PermissionDenied("User has no associated business")

        serializer.save(businessId=business)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        
        # Handle the image file
        image = request.FILES.get('image')
        data = request.data.copy()
        
        # Convert string fields to proper format
        for field in ['equipment', 'tags']:
            if field in data and isinstance(data[field], str):
                try:
                    data[field] = json.loads(data[field])
                except json.JSONDecodeError:
                    data[field] = []

        if image:
            data['image'] = image
            # Delete old image if it exists
            if instance.image:
                instance.image.delete(save=False)
        
        serializer = self.get_serializer(instance, data=data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        
        return Response(serializer.data)
    
    def perform_destroy(self, instance):
        # Delete the image file when deleting the option
        if instance.image:
            instance.image.delete(save=False)
        instance.delete()