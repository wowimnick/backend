from datetime import datetime, time
from silk.profiling.profiler import silk_profile
import json
from django.forms import ValidationError
from rest_framework import generics, viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.permissions import BasePermission, IsAuthenticated, AllowAny

from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch, Case, Sum, When, IntegerField, Q, Subquery, Count
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
    ReviewSerializer,
    ClassOptionSerializer,
    ClassCreateSerializer,
    ScheduleSerializer,
    ClassOptionCreateSerializer,
    ScheduleInstanceSerializer,
    ScheduleBreakSerializer
)

from ..utils.permissions import check_user_role, BaseUserDataPermission
from .utils import haversine_distance

logger = logging.getLogger(__name__)
    
def validate_schedule_conflicts(option, schedule_data, exclude_id=None):
    """
    Validate schedule for time conflicts within an option.
    
    Args:
        option: ClassOption instance
        schedule_data: Dict containing 'day' string and 'time' (string or time object)
        exclude_id: Optional ID to exclude from conflict check (for updates)
    
    Returns:
        (bool, str): Tuple of (is_valid, error_message)
    """
    # Convert schedule time to minutes since midnight for comparison
    def time_to_minutes(time_val):
        if isinstance(time_val, str):
            time_obj = datetime.strptime(time_val, '%H:%M').time()
        elif isinstance(time_val, time):  # Using the properly imported time type
            time_obj = time_val
        else:
            raise ValueError(f"Invalid time format: {time_val}")
        return time_obj.hour * 60 + time_obj.minute

    # Get time range for the proposed schedule
    try:
        proposed_start = time_to_minutes(schedule_data['time'])
        proposed_end = proposed_start + option.duration
        proposed_day = schedule_data['day']

        # Check against existing schedules
        existing_schedules = option.schedules.all()
        if exclude_id:
            existing_schedules = existing_schedules.exclude(id=exclude_id)

        for existing in existing_schedules:
            # Only check schedules on the same day
            if existing.day == proposed_day:
                existing_start = time_to_minutes(existing.time)
                existing_end = existing_start + option.duration

                # Check time overlap
                if (proposed_start < existing_end and proposed_end > existing_start):
                    return False, f"Schedule conflicts with existing class at {existing.time} on {existing.day}"

        return True, None
        
    except (ValueError, KeyError) as e:
        return False, f"Invalid schedule data: {str(e)}"

class BusinessPermissionMixin:
    """Mixin to handle business-specific permissions"""
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
        queryset = ClassesMain.objects.all()
        
        # Add select_related and prefetch_related
        queryset = queryset.select_related('businessId').prefetch_related(
            'options',
            'options__schedules',
            'options__schedules__instances',
            'options__schedules__breaks',  # Add prefetch for breaks
            Prefetch(
                'options__schedules__instances__bookings',
                queryset=Booking.objects.filter(status='confirmed'),
                to_attr='confirmed_bookings'
            )
        )
        
        # The rest of your logic for filtering public/business classes
        if self.is_business_context:
            if check_user_role(self.request.user, ['Admin', 'Super Admin']):
                return queryset
                
            return queryset.filter(
                Q(businessId__owner=self.request.user) |
                Q(businessId__managers=self.request.user)
            ).distinct()
        
        # Public context - show only active classes
        return queryset.filter(
            Exists(
                ClassOption.objects.filter(
                    classId=OuterRef('pk'),
                    active=True
                )
            )
        )

    @silk_profile(name='ClassViewSet_search')
    @action(detail=False, methods=['get'])
    def search(self, request):
        """
        Optimized search endpoint that returns minimal data needed for search results
        """
        try:
            # Get search parameters
            lat = request.query_params.get('lat')
            lng = request.query_params.get('lng')
            radius = float(request.query_params.get('radius', 100))
            location = request.query_params.get('location')
            
            # Define base queryset
            queryset = ClassesMain.objects.select_related(
                'businessId'
            ).prefetch_related(
                'images',
                Prefetch(
                    'options',
                    queryset=ClassOption.objects.filter(active=True),
                    to_attr='active_options'
                )
            ).only(
                'classId', 'title', 'location', 'coordinates',
                'category', 'subcategory', 'studentContactEmail', 
                'studentContactPhone', 'businessId__businessName',
                'businessId__totalReviews'
            )

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

            # Process results
            serialized_data = []
            for cls in queryset:
                class_data = {
                    'classId': cls.classId,
                    'title': cls.title,
                    'location': cls.location,
                    'coordinates': cls.coordinates,
                    'category': cls.category,
                    'subcategory': cls.subcategory,
                    'rating': None,
                    'totalReviews': cls.businessId.totalReviews if cls.businessId else 0,
                    'image': None,
                    'priceRange': {
                        'min': None,
                        'max': None
                    }
                }

                # Get first image if exists
                images = list(cls.images.all())
                if images:
                    class_data['image'] = images[0].image.url

                # Get price range from active options
                active_options = getattr(cls, 'active_options', [])
                if active_options:
                    prices = [option.price for option in active_options if option.price is not None]
                    if prices:
                        class_data['priceRange']['min'] = min(prices)
                        class_data['priceRange']['max'] = max(prices)

                serialized_data.append(class_data)

            return Response({
                'results': serialized_data
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
    def toggle_option_active(self, request, pk=None):
        """Toggle option active status"""
        self.is_business_context = True
        option_id = request.data.get('option_id')
        option = get_object_or_404(ClassOption, optionId=option_id, classId_id=pk)
        
        option.active = not option.active
        option.save()
        
        return Response({
            'active': option.active,
            'optionId': option.optionId
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
        self.is_business_context = True
        instance = self.get_object()
        
        try:
            with transaction.atomic():
                # Delete related data
                Schedule.objects.filter(option__classId=instance).delete()
                ClassOption.objects.filter(classId=instance).delete()
                
                # Delete images
                for image in instance.images.all():
                    if image.image:
                        image.image.delete(save=False)
                    image.delete()
                
                Reviews.objects.filter(classId=instance).delete()
                instance.delete()
                
            return Response(status=status.HTTP_204_NO_CONTENT)
            
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class ClassImageList(generics.ListCreateAPIView):
    serializer_class = ClassImageSerializer

    @silk_profile(name='ClassImageList_get_queryset')
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

class ClassReviews(generics.ListAPIView):
    serializer_class = ReviewSerializer

    def get_queryset(self):
        class_id = self.kwargs['pk']
        return Reviews.objects.filter(classId=class_id).select_related('userId')



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

        # Validate conflicts before creating
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
    
    def validate_conflicts(self, request):
        option_id = request.data.get('option_id')
        schedule_data = request.data
        
        try:
            option = ClassOption.objects.get(id=option_id)
            is_valid, error_message = validate_schedule_conflicts(
                option, 
                schedule_data,
                exclude_id=schedule_data.get('id')
            )
            
            if not is_valid:
                return Response(
                    {'valid': False, 'error': error_message},
                    status=status.HTTP_400_BAD_REQUEST
                )
                
            return Response({'valid': True})
            
        except ClassOption.DoesNotExist:
            return Response(
                {'error': 'Option not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=True, methods=['post'])
    def regenerate_instances(self, request, pk=None):
        """Regenerate future instances for a schedule"""
        schedule = self.get_object()
        start_date = timezone.now().date()
        weeks_ahead = int(request.data.get('weeks_ahead', 12))
        
        try:
            instances = schedule.generate_instances(
                start_date=start_date,
                weeks_ahead=weeks_ahead
            )
            
            return Response({
                'message': f'Generated {len(instances)} new instances',
                'instances': ScheduleInstanceSerializer(instances, many=True).data
            })
            
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
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
        
        if not option_id:
            return Response(
                {'error': 'option_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Convert date strings to date objects
            start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else timezone.now().date()
            end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else (start_date + timezone.timedelta(days=7))

            # Get instances with pre-calculated booking counts in a single query
            instances = ScheduleInstance.objects.filter(
                schedule__option_id=option_id,
                schedule__is_active=True,
                date__range=(start_date, end_date),
                status='scheduled'
            ).annotate(
                current_bookings_count=Coalesce(
                    Sum(
                        Case(
                            When(
                                bookings__status='confirmed',
                                then='bookings__participants'
                            ),
                            default=0
                        )
                    ),
                    0,
                    output_field=IntegerField()
                )
            ).select_related('schedule')

            # Format instances with pre-calculated availability
            available_instances = []
            for instance in instances:
                available_spots = instance.max_participants - instance.current_bookings_count
                
                # Only include if spots are available
                if available_spots > 0:
                    available_instances.append({
                        'instance_id': instance.id,
                        'schedule_id': instance.schedule.id,
                        'date': instance.date,
                        'time': instance.time,
                        'total_capacity': instance.max_participants,
                        'available_spots': available_spots,
                        'price': str(instance.price)
                    })

            return Response({
                'instances': available_instances,
                'total_available': len(available_instances),
                'date_range': {
                    'start_date': start_date_str,
                    'end_date': end_date_str
                }
            })

        except ValueError as e:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
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
            schedule_days = set([day_to_number[schedule.day] for schedule in schedules])

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