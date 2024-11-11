from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.utils.dateparse import parse_date
from django.db.models import Exists, OuterRef
from django.conf import settings

from rest_framework import generics, viewsets, status, permissions, filters
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from dj_rest_auth.registration.views import RegisterView

from .models import (
    Booking, BookingStatus, BusinessInfo, ClassesMain, 
    Enrollment, Reviews, ClassImage, Schedule, Student, 
    SubClasses, Instructor, Role
)
from .serializers import (
    AttendanceSerializer, BookingSerializer, BookingStatusSerializer,
    BusinessInfoSerializer, ClassesMainSerializer, PerformanceSerializer,
    ReviewSerializer, ClassImageSerializer, ScheduleSerializer,
    StudentSerializer, SubClassesSerializer, CustomRegisterSerializer,
    CustomUserDetailsSerializer, InstructorSerializer, EducationSerializer,
    CertificationSerializer, SkillSerializer, InstructorNoteSerializer,
    RoleSerializer, StudentNoteSerializer, EnrollmentSerializer,
    CustomTokenObtainPairSerializer
)

import logging

logger = logging.getLogger(__name__)
logger.debug("views module loaded")

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
        # Add user info to the response
        user_data = {
            'first_name': instance.userId.first_name,
            'last_name': instance.userId.last_name,
            'email': instance.userId.email,
        }
        data['user'] = user_data
        
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
        return request.user and request.user.has_role('Admin') or request.user.has_role('SuperAdmin')

class IsBusinessOwner(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and (request.user.has_role('Business Owner') or request.user.has_role('Admin') or request.user.has_role('SuperAdmin'))

class IsManager(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and (request.user.has_role('Manager') or request.user.has_role('Business Owner') or request.user.has_role('Admin') or request.user.has_role('SuperAdmin'))

class IsInstructor(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and (request.user.has_role('Instructor') or request.user.has_role('Content Creator') or request.user.has_role('Manager') or request.user.has_role('Business Owner') or request.user.has_role('Admin') or request.user.has_role('SuperAdmin'))

class InstructorViewSet(viewsets.ModelViewSet):
    queryset = Instructor.objects.all()
    serializer_class = InstructorSerializer

    def create(self, request, *args, **kwargs):
        logger.info(f"Received create request with data: {request.data}")
        return super().create(request, *args, **kwargs)

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsManager]
        else:
            permission_classes = [IsInstructor]
        return [permission() for permission in permission_classes]

    @action(detail=True, methods=['post'])
    def add_education(self, request, pk=None):
        instructor = self.get_object()
        serializer = EducationSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_certification(self, request, pk=None):
        instructor = self.get_object()
        serializer = CertificationSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_skill(self, request, pk=None):
        instructor = self.get_object()
        serializer = SkillSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        instructor = self.get_object()
        serializer = InstructorNoteSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor, author=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
class BookingStatusViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BookingStatus.objects.all()
    serializer_class = BookingStatusSerializer
    permission_classes = [permissions.IsAuthenticated]

class BookingViewSet(viewsets.ModelViewSet):
    queryset = Booking.objects.all()
    serializer_class = BookingSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            'student__user', 
            'instructor__user',
            'class_instance', 
            'subclass'
        )
        status = self.request.query_params.get('status', None)
        instructor = self.request.query_params.get('instructor', None)

        if status is not None:
            if ',' in status:
                statuses = status.split(',')
                queryset = queryset.filter(status__name__in=statuses)
            else:
                queryset = queryset.filter(status__name=status)

        if instructor is not None:
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

class StudentViewSet(viewsets.ModelViewSet):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsManager]
        else:
            permission_classes = [permissions.IsAuthenticated]
        return [permission() for permission in permission_classes]

    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        student = self.get_object()
        serializer = StudentNoteSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(student=student, author=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def enroll(self, request, pk=None):
        student = self.get_object()
        serializer = EnrollmentSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(student=student)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def record_attendance(self, request, pk=None):
        enrollment = get_object_or_404(Enrollment, pk=request.data.get('enrollment_id'))
        serializer = AttendanceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(enrollment=enrollment)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def record_performance(self, request, pk=None):
        enrollment = get_object_or_404(Enrollment, pk=request.data.get('enrollment_id'))
        serializer = PerformanceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(enrollment=enrollment)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def bookings(self, request, pk=None):
        student = self.get_object()
        bookings = student.bookings.all()
        serializer = BookingSerializer(bookings, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def create_booking(self, request, pk=None):
        student = self.get_object()
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

@require_GET
def get_google_maps_api_key(request):
    return JsonResponse({'key': settings.GOOGLE_MAPS_API_KEY})