from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Prefetch, Q, Count, Value, Case, DecimalField, When, F, ExpressionWrapper, Subquery, OuterRef
from django.db.models.functions import Coalesce
from django.utils import timezone

from quickstart.models import Booking, BusinessInfo, CustomUser, StudentNote

from ..utils.permissions import check_user_role
from ..serializers import StudentProfileSerializer, StudentNoteSerializer

class StudentProfileViewSet(viewsets.ModelViewSet):
    serializer_class = StudentProfileSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        
        # Get the business context
        business = None
        if check_user_role(user, ['Business Owner', 'Manager']):
            business = BusinessInfo.objects.filter(
                Q(owner=user) | Q(managers=user)
            ).first()

        # Prepare subqueries for bookings
        booking_counts = Booking.objects.filter(
            user=OuterRef('pk')
        ).values('user').annotate(
            confirmed_count=Count('id', filter=Q(status='confirmed')),
            completed_count=Count('id', filter=Q(status='completed')),
            finished_count=Count('id', filter=Q(status__in=['completed', 'cancelled']))
        ).values(
            'confirmed_count',
            'completed_count',
            'finished_count'
        )
        
        # Base queryset with efficient joins and annotations
        queryset = CustomUser.objects.select_related(
            'role'
        )
        
        if business:
            # Prefetch only notes for this business
            queryset = queryset.prefetch_related(
                Prefetch(
                    'business_notes',
                    queryset=StudentNote.objects.filter(business=business)
                        .select_related('author')
                        .order_by('-created_at'),
                    to_attr='prefetched_notes'
                )
            )
        
        queryset = queryset.prefetch_related(
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

        # Filter users based on business context
        if business:
            return queryset.filter(
                bookings__schedule_instance__schedule__option__classId__businessId=business
            ).distinct()
        
        return queryset.filter(userId=user.pk)
    
    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        user = self.get_object()
        business = BusinessInfo.objects.filter(
            Q(owner=request.user) | Q(managers=request.user)
        ).first()
        
        if not business:
            return Response(
                {"error": "You must be associated with a business to add notes"},
                status=status.HTTP_403_FORBIDDEN
            )
            
        serializer = StudentNoteSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(
                user=user,
                business=business,
                author=request.user
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)