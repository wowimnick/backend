from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone

from quickstart.models import Student

from .permissions import check_user_role
from ..serializers import StudentProfileSerializer, StudentNoteSerializer

class StudentProfileViewSet(viewsets.ModelViewSet):
    serializer_class = StudentProfileSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        # Admins and staff can see all students
        if check_user_role(user, ['Admin', 'Super Admin']):
            return Student.objects.all()
        # Business owners and managers can see their students
        if check_user_role(user, ['Business Owner', 'Manager']):
            return Student.objects.filter(
                enrollments__class_option__classId__businessId__in=user.managed_businesses.all()
            ).distinct()
        # Regular users can only see themselves
        return Student.objects.filter(user=user)
    
    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        student = self.get_object()
        serializer = StudentNoteSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(
                student=student,
                author=request.user
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['get', 'patch'])
    def me(self, request):
        """Get or update current user's student profile"""
        try:
            student = Student.objects.get(user=request.user)
            if request.method == 'PATCH':
                serializer = self.get_serializer(
                    student, 
                    data=request.data, 
                    partial=True
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
            else:
                serializer = self.get_serializer(student)
            return Response(serializer.data)
        except Student.DoesNotExist:
            if request.method == 'GET':
                return Response(
                    {'detail': 'Student profile not found'}, 
                    status=status.HTTP_404_NOT_FOUND
                )
            # Create new profile with basic info
            serializer = self.get_serializer(data={
                'user': request.user.id,
                'enrollment_date': timezone.now().date(),
                **request.data
            })
            serializer.is_valid(raise_exception=True)
            serializer.save(user=request.user)
            return Response(
                serializer.data, 
                status=status.HTTP_201_CREATED
            )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        # Clear 'Pending Update' values if they're being updated
        instance = serializer.instance
        data = serializer.validated_data
        
        pending_fields = ['parent_guardian_phone', 'emergency_phone']
        for field in pending_fields:
            if field in data and instance.__dict__.get(field) == 'Pending Update':
                # Value is being updated from the default
                pass  # Let the update proceed
            elif field in data and data[field] == 'Pending Update':
                # Don't allow setting back to pending
                data[field] = instance.__dict__[field]
                
        serializer.save()