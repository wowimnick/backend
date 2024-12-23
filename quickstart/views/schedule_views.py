from rest_framework import viewsets, status, filters
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django.utils import timezone
from datetime import timedelta
import logging

from ..models import (
    Schedule, ScheduleStudent
)
from ..serializers import (
    ScheduleSerializer,
    ScheduleStudentSerializer
)
from .permissions import IsInstructor, check_user_role

logger = logging.getLogger(__name__)

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