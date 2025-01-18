from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError

from ..models import (
    Booking,
    Schedule
)
from ..serializers import (
    BookingCreateSerializer,
    BookingDetailSerializer
)

from .permissions import check_user_role
from django.db import models

class BookingViewSet(viewsets.ModelViewSet):
    def get_queryset(self):
        user = self.request.user
        status_filter = self.request.query_params.get('status')
        
        # Start with base queryset
        queryset = Booking.objects.select_related(
            'schedule_instance', 
            'schedule_instance__schedule',
            'schedule_instance__schedule__option',
            'schedule_instance__schedule__option__classId',
            'schedule_instance__schedule__option__classId__businessId',
            'student'
        )
        
        # Handle multiple status filtering
        if status_filter:
            status_values = [s.strip() for s in status_filter.split(',')]
            queryset = queryset.filter(status__in=status_values)
        
        # For business owners/managers - show all bookings for their business
        if check_user_role(user, ['Business Owner', 'Manager']):
            return queryset.filter(
                schedule_instance__schedule__option__classId__businessId__in=user.managed_businesses.all()
            )
            
        # For students - show only their bookings
        return queryset.filter(student__user=user)

    def get_serializer_class(self):
        if self.action == 'create':
            return BookingCreateSerializer
        return BookingDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            booking = serializer.save(
                student=request.user.student_profile,
                status='pending',
                payment_status='pending'
            )
            
            # Here you would integrate with payment processing
            # For now, we'll just confirm the booking
            booking.status = 'confirmed'
            booking.payment_status = 'paid'
            booking.save()
            
            return Response(
                BookingDetailSerializer(booking).data,
                status=status.HTTP_201_CREATED
            )
            
        except DjangoValidationError as e:
            return Response(
                {'error': e.messages},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        
        # Can only cancel confirmed bookings
        if booking.status != 'confirmed':
            return Response(
                {'error': ['Can only cancel confirmed bookings']},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        booking.cancellation_reason = request.data.get('reason', '')
        booking.save()
        
        return Response(BookingDetailSerializer(booking).data)

    @action(detail=False, methods=['get'])
    def schedule_availability(self, request):
        """Get availability for a specific schedule"""
        schedule_id = request.query_params.get('schedule_id')
        if not schedule_id:
            return Response(
                {'error': ['schedule_id is required']},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        try:
            schedule = Schedule.objects.get(id=schedule_id)
            current_bookings = schedule.current_bookings
            
            return Response({
                'schedule_id': schedule_id,
                'total_capacity': schedule.effective_max_participants,
                'booked': current_bookings,
                'available': schedule.effective_max_participants - current_bookings,
                'is_full': current_bookings >= schedule.effective_max_participants
            })
            
        except Schedule.DoesNotExist:
            return Response(
                {'error': ['Schedule not found']},
                status=status.HTTP_404_NOT_FOUND
            )
