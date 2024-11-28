from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.utils.dateparse import parse_date
from django.db.models import Exists, OuterRef
from django.db import connection
from django.conf import settings
from django.utils import timezone
from datetime import datetime, timedelta
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
import math


from rest_framework import generics, viewsets, status, permissions, filters
from rest_framework.response import Response
from rest_framework.decorators import action, api_view
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework.response import Response
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied
from dj_rest_auth.registration.views import RegisterView

from .models import (
    Booking, BookingStatus, BusinessInfo, ClassesMain, 
    Enrollment, Reviews, ClassImage, Schedule, Student, 
    SubClasses, Instructor, Role
)
from .serializers import (
    AttendanceSerializer, BookingSerializer, BookingStatusSerializer,
    BusinessInfoSerializer, BusinessStatsSerializer, ClassesMainSerializer, PerformanceSerializer,
    ReviewSerializer, ClassImageSerializer, ScheduleSerializer,
    StudentSerializer, SubClassesSerializer, CustomRegisterSerializer,
    CustomUserDetailsSerializer, InstructorSerializer, EducationSerializer,
    CertificationSerializer, SkillSerializer, InstructorNoteSerializer,
    RoleSerializer, StudentNoteSerializer, EnrollmentSerializer,
    CustomTokenObtainPairSerializer
)

import logging
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication

logger = logging.getLogger(__name__)

class UserRoleView(APIView):
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            # Get current user role
            role = request.user.role.name if request.user.role else None
            
            # Return role with hierarchy information
            role_hierarchy = {
                'Super Admin': 100,
                'Admin': 90,
                'Business Owner': 80,
                'Manager': 70,
                'Instructor': 60,
                'Content Creator': 50,
                'Student': 10,
                'Guest': 0
            }
            
            return Response({
                'role': role,
                'roleLevel': role_hierarchy.get(str(role) if role else '', 0),
                'allowedRoles': [r for r, level in role_hierarchy.items() 
                               if level <= role_hierarchy.get(str(role) if role else '', 0)]
            })
            
        except Exception as e:
            return Response({
                'error': str(e),
                'role': None,
                'roleLevel': 0,
                'allowedRoles': []
            }, status=400)
        
def check_user_role(user, allowed_roles):
    """
    Check if user has any of the allowed roles
    
    Args:
        user: User object
        allowed_roles: List of role names
    Returns:
        bool: True if user has any of the allowed roles
    """
    logger.debug(f"User: {user.role.name}")
    return user.role and user.role.name in allowed_roles
        
class BaseUserDataPermission(permissions.BasePermission):
    """
    Base permission class for user-related data access
    """
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        # Admin users can access all records
        if check_user_role(request.user, ['Admin', 'Super Admin']):
            return True
            
        # Check if the object belongs to the requesting user
        if hasattr(obj, 'user'):
            return obj.user == request.user
        if hasattr(obj, 'userId'):
            return obj.userId == request.user
        return False

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)

        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            raise InvalidToken(e.args[0])

        data = serializer.validated_data

        # Set refresh token in HTTP-only cookie
        response = Response(data, status=status.HTTP_200_OK)
        response.set_cookie(
            key='refresh_token',
            value=data['refresh'],
            max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
            httponly=True,
            samesite='lax',
            secure=settings.SESSION_COOKIE_SECURE,  # True in production
            path='/api/token/refresh/'
        )

        # Remove refresh token from response data
        del data['refresh']
        
        return response
    
class CustomTokenRefreshView(APIView):
    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get('refresh_token')

        if not refresh_token:
            return Response(
                {"detail": "No refresh token provided"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        try:
            refresh = RefreshToken(refresh_token)
            data = {
                'access': str(refresh.access_token)
            }

            if settings.SIMPLE_JWT['ROTATE_REFRESH_TOKENS']:
                # Create new refresh token
                new_refresh = RefreshToken.for_user(refresh.user)
                
                response = Response(data, status=status.HTTP_200_OK)
                response.set_cookie(
                    key='refresh_token',
                    value=str(new_refresh),
                    max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
                    httponly=True,
                    samesite='Lax',
                    secure=settings.SESSION_COOKIE_SECURE,
                    path='/api/token/refresh/'
                )
                
                if settings.SIMPLE_JWT['BLACKLIST_AFTER_ROTATION']:
                    try:
                        # Blacklist the old refresh token
                        refresh.blacklist()
                    except AttributeError:
                        pass

                return response

            return Response(data, status=status.HTTP_200_OK)

        except (TokenError, AttributeError, TypeError) as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_401_UNAUTHORIZED

            )
        
class UserUpdateView(APIView):
    def patch(self, request):
        serializer = CustomUserDetailsSerializer(
            request.user,
            data=request.data,
            partial=True
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
class LogoutView(APIView):
    def post(self, request):
        try:
            refresh_token = request.COOKIES.get('refresh_token')
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
            
            response = Response(status=status.HTTP_205_RESET_CONTENT)
            response.delete_cookie(
                'refresh_token',
                path='/api/token/refresh/',
                samesite='Lax'
            )
            return response
            
        except Exception:
            return Response(status=status.HTTP_400_BAD_REQUEST)
    
class CustomLoginView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

class CustomRegisterView(RegisterView):
    serializer_class = CustomRegisterSerializer

    def post(self, request, *args, **kwargs):
        logger.debug(f"Registration request data: {request.data}")
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            logger.error(f"Serializer errors: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        return super().post(request, *args, **kwargs)
    
class FavoriteClasses(APIView):
    def get(self, request):
        user = request.user
        if not user.is_authenticated:
            return Response({"error": "Authentication required"}, status=401)
        favorites = user.favorite_classes.all()
        serializer = ClassesMainSerializer(favorites, many=True)
        return Response(serializer.data)

    def post(self, request):
        user = request.user
        if not user.is_authenticated:
            return Response({"error": "Authentication required"}, status=401)
        class_id = request.data.get('class_id')
        if not class_id:
            return Response({"error": "class_id is required"}, status=400)
        class_instance = get_object_or_404(ClassesMain, pk=class_id)
        user.favorite_classes.add(class_instance)
        return Response({"message": "Class added to favorites"})
    
    def delete(self, request):
        user = request.user
        if not user.is_authenticated:
            return Response({"error": "Authentication required"}, status=401)
        class_id = request.data.get('class_id')
        if not class_id:
            return Response({"error": "class_id is required"}, status=400)
        class_instance = get_object_or_404(ClassesMain, pk=class_id)
        user.favorite_classes.remove(class_instance)
        return Response({"message": "Class removed from favorites"})
    
def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the distance between two points on earth using Haversine formula
    Returns distance in kilometers
    """
    R = 6371  # Earth's radius in kilometers

    # Convert latitude and longitude to radians
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    
    # Haversine formula
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    return R * c

@api_view(['GET'])
def search_classes_by_location(request):
    """
    Search for classes based on coordinates within a radius.
    Required parameters: lat, lng
    Optional parameter: radius (in km, defaults to 10)
    """
    try:
        lat = request.GET.get('lat')
        lng = request.GET.get('lng')
        radius = float(request.GET.get('radius', 100))  # Default 10km radius
        
        if not lat or not lng:
            return Response({
                "error": "Missing coordinates",
                "message": "Both latitude and longitude are required"
            }, status=400)
            
        search_lat = float(lat)
        search_lng = float(lng)

        # Get all classes
        classes = ClassesMain.objects.filter(isActive=True)
        
        # Filter and add distance
        classes_with_distance = []
        for class_obj in classes:
            try:
                if not class_obj.classCoordinates:
                    continue
                    
                class_lat, class_lng = map(
                    float, 
                    class_obj.classCoordinates.split(',')
                )
                
                distance = haversine_distance(
                    search_lat, search_lng,
                    class_lat, class_lng
                )
                
                if distance <= radius:
                    classes_with_distance.append((class_obj, distance))
                        
            except (ValueError, AttributeError) as e:
                logger.warning(f"Error processing class coordinates for class {class_obj.classId}: {str(e)}")
                continue

        # Sort by distance
        classes_with_distance.sort(key=lambda x: x[1])
        
        # Prepare response
        serialized_classes = []
        for class_obj, distance in classes_with_distance:
            class_data = ClassesMainSerializer(class_obj).data
            class_data['distance'] = round(distance, 2)
            class_data['distance_text'] = (
                f"{round(distance, 1)}km away" if distance >= 1 
                else f"{round(distance * 1000)}m away"
            )
            serialized_classes.append(class_data)

        return Response({
            'results': serialized_classes,
            'total': len(serialized_classes),
            'search_coordinates': {
                'lat': search_lat,
                'lng': search_lng
            }
        })

    except ValueError as e:
        return Response({
            "error": "Invalid coordinates",
            "message": str(e)
        }, status=400)
    except Exception as e:
        logger.error(f"Unexpected error in search_classes_by_location: {str(e)}")
        return Response({
            "error": "Server error",
            "message": "An unexpected error occurred while processing your request"
        }, status=500)

class ClassList(generics.ListCreateAPIView):
    serializer_class = ClassesMainSerializer

    def get_queryset(self):
        queryset = ClassesMain.objects.all()
        
        private = self.request.query_params.get('private', None)
        group = self.request.query_params.get('group', None)

        if private == 'true':
            queryset = queryset.filter(
                Exists(SubClasses.objects.filter(classId=OuterRef('pk'), subclassType='Private'))
            )
        elif group == 'true':
            queryset = queryset.filter(
                Exists(SubClasses.objects.filter(classId=OuterRef('pk'), subclassType='Group'))
            )

        return queryset

    def perform_create(self, serializer):
        instance = serializer.save(
            classVideo=self.request.FILES.get('classVideo')
        )
        images = self.request.FILES.getlist('classImages')
        for image in images:
            ClassImage.objects.create(classId=instance, image=image)

class ClassDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = ClassesMain.objects.all()
    serializer_class = ClassesMainSerializer

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if 'classImages' in request.FILES:
            images = request.FILES.getlist('classImages')
            for image in images:
                ClassImage.objects.create(classId=instance, image=image)

        if getattr(instance, '_prefetched_objects_cache', None):
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)

class ClassImageList(generics.ListCreateAPIView):
    serializer_class = ClassImageSerializer

    def get_queryset(self):
        return ClassImage.objects.filter(classId=self.kwargs['pk'])

    def perform_create(self, serializer):
        class_instance = ClassesMain.objects.get(pk=self.kwargs['pk'])
        serializer.save(classId=class_instance)

class ClassImageDetail(generics.RetrieveDestroyAPIView):
    queryset = ClassImage.objects.all()
    serializer_class = ClassImageSerializer


class ClassReviews(generics.ListAPIView):
    serializer_class = ReviewSerializer

    def get_queryset(self):
        class_id = self.kwargs['pk']
        return Reviews.objects.filter(classId=class_id).select_related('userId')

class SubClassesViewSet(generics.ListCreateAPIView):
    serializer_class = SubClassesSerializer

    def get_queryset(self):
        class_id = self.kwargs['pk']
        return SubClasses.objects.filter(classId=class_id)

    def perform_create(self, serializer):
        class_instance = ClassesMain.objects.get(pk=self.kwargs['pk'])
        serializer.save(classId=class_instance)
    
class SubClassDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = SubClasses.objects.all()
    serializer_class = SubClassesSerializer

    def get_object(self):
        queryset = self.get_queryset()
        subclass_id = self.kwargs.get('subclass_id')
        obj = get_object_or_404(queryset, subclassId=subclass_id)
        self.check_object_permissions(self.request, obj)
        return obj

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if 'subclassImage' in request.FILES:
            instance.subclassImage = request.FILES['subclassImage']
            instance.save()

        return Response(serializer.data)
    
class BusinessViewSet(viewsets.ModelViewSet):
    serializer_class = BusinessStatsSerializer
    permission_classes = [permissions.IsAuthenticated]

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
        
        # Get performance metrics for each class
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
    
class BusinessInfoDetail(generics.RetrieveUpdateDestroyAPIView):
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
        
        # Add related classes
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
    
class IsAdminUser(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and check_user_role(request.user, ['Admin', 'Super Admin'])

class IsBusinessOwner(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and check_user_role(
            request.user, 
            ['Business Owner', 'Admin', 'Super Admin']
        )

class IsManager(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and check_user_role(
            request.user,
            ['Manager', 'Business Owner', 'Admin', 'Super Admin']
        )

class IsInstructor(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and check_user_role(
            request.user,
            ['Instructor', 'Content Creator', 'Manager', 'Business Owner', 'Admin', 'Super Admin']
        )

class SecureInstructorViewSet(viewsets.ModelViewSet):
    queryset = Instructor.objects.all()
    serializer_class = InstructorSerializer
    permission_classes = [BaseUserDataPermission]

    def get_queryset(self):
        user = self.request.user
        if user.has_role('Admin') or user.has_role('Super Admin'):
            return Instructor.objects.all()
        elif user.has_role('Business Owner'):
            return Instructor.objects.filter(business__owner=user)
        elif user.has_role('Manager'):
            return Instructor.objects.filter(business__in=user.managed_businesses.all())
        elif user.has_role('Instructor'):
            return Instructor.objects.filter(user=user)
        return Instructor.objects.none()

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsManager]
        else:
            permission_classes = [IsInstructor]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        # Verify business access if specified
        business_id = self.request.data.get('business')
        if business_id:
            business = get_object_or_404(BusinessInfo, id=business_id)
            if not (self.request.user.has_role('Admin') or 
                   business.owner == self.request.user or 
                   business in self.request.user.managed_businesses.all()):
                raise PermissionDenied("You don't have permission to add instructors to this business")
        serializer.save()

    @action(detail=True, methods=['post'])
    def add_education(self, request, pk=None):
        instructor = self.get_object()
        if not (request.user.has_role('Admin') or instructor.user == request.user):
            raise PermissionDenied("You don't have permission to add education to this instructor")
        serializer = EducationSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_certification(self, request, pk=None):
        instructor = self.get_object()
        if not (request.user.has_role('Admin') or instructor.user == request.user):
            raise PermissionDenied("You don't have permission to add certifications to this instructor")
        serializer = CertificationSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_skill(self, request, pk=None):
        instructor = self.get_object()
        if not (request.user.has_role('Admin') or instructor.user == request.user):
            raise PermissionDenied("You don't have permission to add skills to this instructor")
        serializer = SkillSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        instructor = self.get_object()
        if not (request.user.has_role('Admin') or request.user.has_role('Manager')):
            raise PermissionDenied("You don't have permission to add notes")
        serializer = InstructorNoteSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor, author=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
class BookingStatusViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BookingStatus.objects.all()
    serializer_class = BookingStatusSerializer
    permission_classes = [IsInstructor]

class BookingViewSet(viewsets.ModelViewSet):
    queryset = Booking.objects.all()
    serializer_class = BookingSerializer
    permission_classes = [IsInstructor]

    

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            'student__user', 
            'instructor__user',
            'class_instance', 
            'subclass'
        )

        user = self.request.user
        if check_user_role(user, ['Admin', 'Super Admin']):
            pass
        elif check_user_role(user, ['Business Owner']):
            queryset = queryset.filter(class_instance__businessId__owner=user)
        elif check_user_role(user, ['Manager']):
            managed_businesses = user.managed_businesses.all()
            queryset = queryset.filter(class_instance__businessId__in=managed_businesses)
        elif check_user_role(user, ['Instructor']):
            queryset = queryset.filter(instructor__user=user)
        else:
            queryset = queryset.none()

        status = self.request.query_params.get('status', None)
        instructor = self.request.query_params.get('instructor', None)

        if status:
            if ',' in status:
                statuses = status.split(',')
                queryset = queryset.filter(status__name__in=statuses)
            else:
                queryset = queryset.filter(status__name=status)

        if instructor:
            queryset = queryset.filter(instructor_id=instructor)

        return queryset

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        
        # Convert class_date to proper format if it exists
        if 'class_date' in request.data:
            try:
                request.data['class_date'] = datetime.strptime(
                    request.data['class_date'],
                    '%Y-%m-%d %H:%M:%S'
                )
            except ValueError:
                return Response(
                    {"error": "Invalid date format. Use YYYY-MM-DD HH:MM:SS"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if getattr(instance, '_prefetched_objects_cache', None):
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)

    def perform_update(self, serializer):
        serializer.save()


class SecureStudentViewSet(viewsets.ModelViewSet):
    serializer_class = StudentSerializer
    permission_classes = [BaseUserDataPermission]

    def get_queryset(self):
        user = self.request.user
        if check_user_role(user, ['Admin', 'Super Admin']):
            return Student.objects.all()
        elif check_user_role(user, ['Business Owner']):
            return Student.objects.filter(
                Q(enrollments__class_instance__businessId__owner=user) |
                Q(bookings__class_instance__businessId__owner=user)
            ).distinct()
        elif check_user_role(user, ['Manager']):
            managed_businesses = user.managed_businesses.all()
            return Student.objects.filter(
                Q(enrollments__class_instance__businessId__in=managed_businesses) |
                Q(bookings__class_instance__businessId__in=managed_businesses)
            ).distinct()
        elif check_user_role(user, ['Instructor']):
            return Student.objects.filter(
                Q(enrollments__class_instance__instructor__user=user) |
                Q(bookings__instructor__user=user)
            ).distinct()
        return Student.objects.filter(user=user)

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsManager]
        else:
            permission_classes = [permissions.IsAuthenticated]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        if not check_user_role(self.request.user, ['Admin']):
            if serializer.validated_data.get('user') != self.request.user:
                raise PermissionDenied("You can only create a student profile for yourself")
        serializer.save()

    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        student = self.get_object()
        if not check_user_role(request.user, ['Admin', 'Manager']):
            raise PermissionDenied("You don't have permission to add notes")
        serializer = StudentNoteSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(student=student, author=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def enroll(self, request, pk=None):
        student = self.get_object()
        class_instance = get_object_or_404(ClassesMain, pk=request.data.get('class_instance'))
        
        if not (check_user_role(request.user, ['Admin']) or 
                student.user == request.user or 
                class_instance.businessId.owner == request.user):
            raise PermissionDenied("You don't have permission to enroll this student")
            
        serializer = EnrollmentSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(student=student)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def record_attendance(self, request, pk=None):
        enrollment = get_object_or_404(Enrollment, pk=request.data.get('enrollment_id'))
        
        # Verify attendance recording permissions
        if not (request.user.has_role('Admin') or 
                enrollment.class_instance.instructor.user == request.user):
            raise PermissionDenied("You don't have permission to record attendance")
            
        serializer = AttendanceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(enrollment=enrollment)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def record_performance(self, request, pk=None):
        enrollment = get_object_or_404(Enrollment, pk=request.data.get('enrollment_id'))
        
        # Verify performance recording permissions
        if not (request.user.has_role('Admin') or 
                enrollment.class_instance.instructor.user == request.user):
            raise PermissionDenied("You don't have permission to record performance")
            
        serializer = PerformanceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(enrollment=enrollment)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def bookings(self, request, pk=None):
        student = self.get_object()
        # Filter bookings based on user permissions
        if request.user.has_role('Admin'):
            bookings = student.bookings.all()
        elif student.user == request.user:
            bookings = student.bookings.all()
        else:
            bookings = student.bookings.filter(
                Q(class_instance__businessId__owner=request.user) |
                Q(instructor__user=request.user)
            )
        serializer = BookingSerializer(bookings, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def create_booking(self, request, pk=None):
        student = self.get_object()
        # Verify booking creation permissions
        if not (request.user.has_role('Admin') or student.user == request.user):
            raise PermissionDenied("You don't have permission to create bookings for this student")
            
        serializer = BookingSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(student=student)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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
    
class ScheduleViewSet(viewsets.ModelViewSet):
    queryset = Schedule.objects.all()
    serializer_class = ScheduleSerializer
    permission_classes = [IsInstructor]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['class_instance__className', 'instructor__user__first_name', 'instructor__user__last_name']
    ordering_fields = ['date', 'start_time', 'created_at']

    def get_queryset(self):
        queryset = super().get_queryset()
        
        user = self.request.user
        if not check_user_role(user, ['Admin', 'Super Admin']):
            if check_user_role(user, ['Business Owner']):
                queryset = queryset.filter(class_instance__businessId__owner=user)
            elif check_user_role(user, ['Manager']):
                queryset = queryset.filter(class_instance__businessId__in=user.managed_businesses.all())
            elif check_user_role(user, ['Instructor']):
                queryset = queryset.filter(instructor__user=user)

        # Apply filters
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        instructor = self.request.query_params.get('instructor')
        status = self.request.query_params.get('status')
        room = self.request.query_params.get('room')

        if date_from:
            queryset = queryset.filter(date__gte=date_from)
        if date_to:
            queryset = queryset.filter(date__lte=date_to)
        if instructor:
            queryset = queryset.filter(instructor_id=instructor)
        if status:
            queryset = queryset.filter(status=status)
        if room:
            queryset = queryset.filter(room=room)

        return queryset

    @action(detail=True, methods=['post'])
    def add_student(self, request, pk=None):
        schedule = self.get_object()
        if not check_user_role(request.user, ['Admin', 'Manager', 'Instructor']):
            raise PermissionDenied("You don't have permission to add students to schedules")
            
        serializer = ScheduleStudentSerializer(data=request.data)
        if serializer.is_valid():
            if schedule.enrolled_students >= schedule.capacity:
                return Response(
                    {"error": "Schedule is at full capacity"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            serializer.save(schedule=schedule)
            schedule.enrolled_students += 1
            schedule.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def remove_student(self, request, pk=None):
        schedule = self.get_object()
        student_id = request.data.get('student_id')
        try:
            schedule_student = ScheduleStudent.objects.get(
                schedule=schedule,
                student_id=student_id
            )
            schedule_student.delete()
            schedule.enrolled_students -= 1
            schedule.save()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except ScheduleStudent.DoesNotExist:
            return Response(
                {"error": "Student not found in schedule"},
                status=status.HTTP_404_NOT_FOUND
            )

    @action(detail=True, methods=['post'])
    def mark_attendance(self, request, pk=None):
        schedule = self.get_object()
        if not (check_user_role(request.user, ['Admin']) or 
                schedule.instructor.user == request.user):
            raise PermissionDenied("You don't have permission to mark attendance")
            
        serializer = ScheduleStudentSerializer(data=request.data, partial=True)
        if serializer.is_valid():
            try:
                schedule_student = ScheduleStudent.objects.get(
                    schedule=schedule,
                    student_id=request.data.get('student')
                )
                schedule_student.status = request.data.get('status')
                schedule_student.attendance_time = timezone.now()
                schedule_student.notes = request.data.get('notes', '')
                schedule_student.save()
                return Response(ScheduleStudentSerializer(schedule_student).data)
            except ScheduleStudent.DoesNotExist:
                return Response(
                    {"error": "Student not found in schedule"},
                    status=status.HTTP_404_NOT_FOUND
                )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            schedule = serializer.save()
            
            # Handle recurring schedules
            if schedule.is_recurring:
                current_date = schedule.date
                if schedule.recurrence_pattern == 'daily':
                    delta = timedelta(days=1)
                elif schedule.recurrence_pattern == 'weekly':
                    delta = timedelta(weeks=1)
                else:  # monthly
                    delta = timedelta(days=30)  # Approximate

                while current_date <= schedule.recurrence_end_date:
                    current_date += delta
                    Schedule.objects.create(
                        class_instance=schedule.class_instance,
                        subclass=schedule.subclass,
                        instructor=schedule.instructor,
                        date=current_date,
                        start_time=schedule.start_time,
                        end_time=schedule.end_time,
                        status=schedule.status,
                        room=schedule.room,
                        capacity=schedule.capacity,
                        notes=schedule.notes,
                        is_recurring=False  # Prevent infinite recursion
                    )

            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'])
    def bulk_create(self, request):
        serializer = self.get_serializer(data=request.data, many=True)
        if serializer.is_valid():
            self.perform_bulk_create(serializer)
            headers = self.get_success_headers(serializer.data)
            return Response(
                serializer.data, 
                status=status.HTTP_201_CREATED,
                headers=headers
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def perform_bulk_create(self, serializer):
        serializer.save()

    @action(detail=False, methods=['post'])
    def bulk_update(self, request):
        serializer = self.get_serializer(data=request.data, many=True)
        if serializer.is_valid():
            self.perform_bulk_update(serializer)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def perform_bulk_update(self, serializer):
        serializer.save()

    @action(detail=False, methods=['get'])
    def by_date(self, request, date):
        try:
            parsed_date = parse_date(date)
            if not parsed_date:
                return Response(
                    {"error": "Invalid date format. Use YYYY-MM-DD"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            queryset = self.get_queryset().filter(date=parsed_date)
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        except ValueError:
            return Response(
                {"error": "Invalid date format"},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=False, methods=['get'])
    def by_instructor(self, request, instructor_id):
        queryset = self.get_queryset().filter(instructor_id=instructor_id)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def by_room(self, request, room):
        queryset = self.get_queryset().filter(room=room)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def get_success_headers(self, data):
        try:
            return {'Location': data[0]['id']}
        except (TypeError, KeyError, IndexError):
            return {}

class RoleViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        
        if check_user_role(self.request.user, ['Admin', 'Super Admin']):
            return Role.objects.all()
        return Role.objects.none()

@require_GET
def get_google_maps_api_key(request):
    return JsonResponse({'key': settings.GOOGLE_MAPS_API_KEY})