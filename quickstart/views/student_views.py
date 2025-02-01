from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Prefetch, Q
from django.utils import timezone

from quickstart.models import Booking, BusinessInfo, Student

from .permissions import check_user_role
from ..serializers import StudentProfileSerializer, StudentNoteSerializer

class StudentProfileViewSet(viewsets.ModelViewSet):
    serializer_class = StudentProfileSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        
        # Business owners and managers can see their students
        if check_user_role(user, ['Business Owner', 'Manager']):
            # Include businesses where the user is either owner or manager
            managed_businesses = BusinessInfo.objects.filter(
                Q(owner=user) | Q(managers=user)
            )
            
            queryset = Student.objects.filter(
                Q(bookings__schedule_instance__schedule__option__classId__businessId__in=managed_businesses)
            ).select_related('user').prefetch_related(
                'bookings',
                'enrollments'
            ).distinct()
            
            return queryset
    
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
            # Try to get existing student profile
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
            # Create new student profile with default values
            if request.method == 'GET':
                # For GET requests, create a basic profile
                new_student_data = {
                    'enrollment_date': timezone.now().date(),
                    'parent_guardian_name': '',
                    'parent_guardian_phone': '',
                    'emergency_contact': '',
                    'emergency_phone': '',
                    'allergies': '',
                    'medical_conditions': ''
                }
            else:
                # For PATCH requests, use provided data
                new_student_data = {
                    'enrollment_date': timezone.now().date(),
                    **request.data
                }
            
            # Create new student profile
            serializer = self.get_serializer(
                data=new_student_data,
                context={'user': request.user}  # Pass user in context
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            
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