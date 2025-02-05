from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Prefetch, Q, Count, Value, Case, DecimalField, When, F, ExpressionWrapper, Subquery, OuterRef
from django.db.models.functions import Coalesce
from django.utils import timezone

from quickstart.models import Booking, BusinessInfo, Student, StudentEnrollment, StudentNote

from ..utils.permissions import check_user_role
from ..serializers import StudentProfileSerializer, StudentNoteSerializer

class StudentProfileViewSet(viewsets.ModelViewSet):
    serializer_class = StudentProfileSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user

        # Prepare subqueries for bookings
        booking_counts = Booking.objects.filter(
            student=OuterRef('pk')
        ).values('student').annotate(
            confirmed_count=Count('id', filter=Q(status='confirmed')),
            completed_count=Count('id', filter=Q(status='completed')),
            finished_count=Count('id', filter=Q(status__in=['completed', 'cancelled']))
        ).values(
            'confirmed_count',
            'completed_count',
            'finished_count'
        )
        
        # Base queryset with efficient joins and annotations
        queryset = Student.objects.select_related(
            'user',
            'user__role'
        ).prefetch_related(
            # Prefetch notes with author data
            Prefetch(
                'notes',
                queryset=StudentNote.objects.select_related('author').order_by('-created_at'),
                to_attr='prefetched_notes'
            ),
            # Prefetch enrollments with class data
            Prefetch(
                'enrollments',
                queryset=StudentEnrollment.objects.select_related(
                    'class_option',
                    'class_option__classId'
                ).order_by('-created_at'),
                to_attr='prefetched_enrollments'
            ),
            # Prefetch bookings with all related data
            Prefetch(
                'bookings',
                queryset=Booking.objects.select_related(
                    'schedule_instance',
                    'schedule_instance__schedule',
                    'schedule_instance__schedule__option',
                    'schedule_instance__schedule__option__classId'
                ).order_by('-booking_date'),
                to_attr='prefetched_bookings'
            )
        ).annotate(
            # Use subquery for counts to avoid multiple queries
            active_bookings_count=Coalesce(
                Subquery(booking_counts.values('confirmed_count')[:1]),
                Value(0)
            ),
            completed_bookings_count=Coalesce(
                Subquery(booking_counts.values('completed_count')[:1]),
                Value(0)
            ),
            total_finished_bookings=Coalesce(
                Subquery(booking_counts.values('finished_count')[:1]),
                Value(0)
            ),
            # Calculate attendance rate in the same query
            attendance_rate=ExpressionWrapper(
                Case(
                    When(
                        total_finished_bookings__gt=0,
                        then=100.0 * F('completed_bookings_count') / F('total_finished_bookings')
                    ),
                    default=Value(0.0)
                ),
                output_field=DecimalField(max_digits=5, decimal_places=2)
            )
        )

        # Apply role-based filtering
        if check_user_role(user, ['Business Owner', 'Manager']):
            managed_businesses = BusinessInfo.objects.filter(
                Q(owner=user) | Q(managers=user)
            ).values('businessId')
            
            return queryset.filter(
                bookings__schedule_instance__schedule__option__classId__businessId__in=Subquery(managed_businesses)
            ).distinct()
        
        return queryset.filter(user=user)
    
    @action(detail=False, methods=['get', 'patch'])
    def me(self, request):
        """Get or update current user's student profile"""
        try:
            student = self.get_queryset().get(user=request.user)
            
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
            new_student_data = {
                'enrollment_date': timezone.now().date(),
                'parent_guardian_name': '',
                'parent_guardian_phone': '',
                'emergency_contact': '',
                'emergency_phone': '',
                'allergies': '',
                'medical_conditions': '',
                **(request.data if request.method == 'PATCH' else {})
            }
            
            serializer = self.get_serializer(
                data=new_student_data,
                context={'user': request.user}
            )
            serializer.is_valid(raise_exception=True)
            serializer.save(user=request.user)
            
            return Response(
                serializer.data,
                status=status.HTTP_201_CREATED
            )
    
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