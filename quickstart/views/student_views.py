from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.db.models import Q

from ..models import (
    Student, ClassesMain, Enrollment, 
    StudentNote, Booking, BusinessInfo
)
from ..serializers import (
    StudentSerializer, StudentNoteSerializer,
    AttendanceSerializer, PerformanceSerializer,
    EnrollmentSerializer, BookingSerializer
)
from .permissions import (
    BaseUserDataPermission, IsManager, permissions,
    check_user_role
)

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
            # Changed managed_businesses access to a related name from your model
            managed_businesses = BusinessInfo.objects.filter(managers=user)
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
        
        if not (check_user_role(request.user, ['Admin']) or 
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
        
        if not (check_user_role(request.user, ['Admin']) or 
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
        if check_user_role(request.user, ['Admin']):
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
        if not (check_user_role(request.user, ['Admin']) or student.user == request.user):
            raise PermissionDenied("You don't have permission to create bookings for this student")
            
        serializer = BookingSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(student=student)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)