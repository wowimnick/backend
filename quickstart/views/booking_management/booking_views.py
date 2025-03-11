from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count
from datetime import timedelta

from ...models import Booking, ScheduleInstance, Payment
from ...serializers.booking_management.payment_serializers import AdminBookingListSerializer, AdminBookingPaymentSerializer
from ...utils.permissions import IsAdminUser

import logging
import csv
from django.http import HttpResponse

logger = logging.getLogger(__name__)

class AdminBookingViewSet(viewsets.ModelViewSet):
    """Admin-only viewset for managing bookings"""
    permission_classes = [IsAuthenticated, IsAdminUser]
    http_method_names = ['get', 'post', 'patch', 'delete']
    
    def get_serializer_class(self):
        if self.action == 'list':
            return AdminBookingListSerializer
        from ...serializers import BookingDetailSerializer
        return BookingDetailSerializer
    
    def get_queryset(self):
        queryset = Booking.objects.select_related(
            'schedule_instance__schedule__option__classId__businessId',
            'user'
        ).prefetch_related(
            'payments' 
        ).all()
        
        # Apply filters
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
            
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search) |
                Q(user__email__icontains=search) |
                Q(schedule_instance__schedule__option__classId__title__icontains=search) |
                Q(schedule_instance__schedule__option__classId__businessId__businessName__icontains=search) |
                Q(payments__stripe_payment_intent_id__icontains=search)
            ).distinct()
            
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date and end_date:
            queryset = queryset.filter(
                schedule_instance__date__range=[start_date, end_date]
            )
            
        # Apply ordering
        ordering = self.request.query_params.get('ordering', '-booking_date')
        if ordering == 'user_name':
            queryset = queryset.order_by('user__first_name', 'user__last_name')
        elif ordering == '-user_name':
            queryset = queryset.order_by('-user__first_name', '-user__last_name')
        elif ordering == 'class_name':
            queryset = queryset.order_by('schedule_instance__schedule__option__classId__title')
        elif ordering == '-class_name':
            queryset = queryset.order_by('-schedule_instance__schedule__option__classId__title')
        elif ordering == 'date':
            queryset = queryset.order_by('schedule_instance__date', 'schedule_instance__time')
        elif ordering == '-date':
            queryset = queryset.order_by('-schedule_instance__date', '-schedule_instance__time')
        else:
            queryset = queryset.order_by(ordering)
            
        return queryset
    
    def retrieve(self, request, *args, **kwargs):
        """Enhanced retrieve method that includes payment information"""
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data
        
        # Add payment information if available
        payment = instance.payments.first()
        if payment:
            data['payment'] = AdminBookingPaymentSerializer(payment).data
            
        return Response(data)
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Admin cancel booking action"""
        booking = self.get_object()
        
        if booking.status != 'confirmed':
            return Response(
                {'error': 'Only confirmed bookings can be cancelled'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        reason = request.data.get('reason', 'Cancelled by administrator')
        refund = request.data.get('refund', False)
        
        with transaction.atomic():
            # Update booking status
            booking.status = 'cancelled'
            booking.cancelled_at = timezone.now()
            booking.cancellation_reason = reason
            booking.save()
            
            payment = None
            # Process refund if requested
            if refund:
                payment = Payment.objects.filter(booking=booking).first()
                if payment and payment.status == 'succeeded':
                    # In a real implementation, you'd call Stripe here
                    payment.status = 'refunded'
                    payment.refunded_amount = payment.amount
                    payment.refund_date = timezone.now()
                    payment.refund_reason = reason
                    payment.save()
                    
                    # Update booking payment status
                    booking.payment_status = 'refunded'
                    booking.save(update_fields=['payment_status'])
            
        # Return updated booking
        serializer = self.get_serializer(booking)
        data = serializer.data
        
        # Add payment information if available
        if not payment:
            payment = Payment.objects.filter(booking=booking).first()
            
        if payment:
            data['payment'] = AdminBookingPaymentSerializer(payment).data
            
        return Response(data)

    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """Get booking analytics for admin dashboard"""
        try:
            # Get date range
            end_date = timezone.now()
            start_date = end_date - timedelta(days=30)
            if request.query_params.get('start_date') and request.query_params.get('end_date'):
                start_date = timezone.datetime.strptime(request.query_params.get('start_date'), '%Y-%m-%d')
                end_date = timezone.datetime.strptime(request.query_params.get('end_date'), '%Y-%m-%d')
            
            # Get bookings in this period
            bookings = Booking.objects.filter(booking_date__range=[start_date, end_date])
            
            # Calculate basic metrics
            total_bookings = bookings.count()
            confirmed_bookings = bookings.filter(status='confirmed').count()
            completed_bookings = bookings.filter(status='completed').count()
            cancelled_bookings = bookings.filter(status='cancelled').count()
            
            # Calculate revenue
            total_revenue = bookings.filter(
                status__in=['confirmed', 'completed']
            ).aggregate(
                total=Sum('amount_paid')
            )['total'] or 0
            
            # Calculate cancellation rate
            cancellation_rate = 0
            if total_bookings > 0:
                cancellation_rate = (cancelled_bookings / total_bookings) * 100
                
            # Return analytics
            return Response({
                'total_bookings': total_bookings,
                'confirmed_bookings': confirmed_bookings,
                'completed_bookings': completed_bookings,
                'cancelled_bookings': cancelled_bookings,
                'cancellation_rate': round(cancellation_rate, 1),
                'total_revenue': float(total_revenue)
            })
            
        except Exception as e:
            logger.error(f"Error getting booking analytics: {str(e)}", exc_info=True)
            return Response(
                {'error': 'Failed to retrieve booking analytics'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export bookings data to CSV"""
        try:
            # Get filtered queryset
            queryset = self.get_queryset()
            
            # Create CSV response
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="bookings_data.csv"'
            
            # Create CSV writer
            writer = csv.writer(response)
            
            # Write header
            writer.writerow([
                'Booking ID', 'Student Name', 'Student Email', 'Class',
                'Business', 'Date', 'Time', 'Status', 'Payment Status',
                'Amount Paid', 'Participants', 'Booking Date', 'Payment ID'
            ])
            
            # Write data
            for booking in queryset:
                payment_id = ""
                payment = booking.payments.first()
                if payment:
                    payment_id = payment.stripe_payment_intent_id
                    
                writer.writerow([
                    booking.id,
                    f"{booking.user.first_name} {booking.user.last_name}".strip(),
                    booking.user.email,
                    booking.schedule_instance.schedule.option.classId.title,
                    booking.schedule_instance.schedule.option.classId.businessId.businessName,
                    booking.schedule_instance.date,
                    booking.schedule_instance.time,
                    booking.status,
                    booking.payment_status,
                    booking.amount_paid,
                    booking.participants,
                    booking.booking_date.strftime('%Y-%m-%d %H:%M:%S'),
                    payment_id
                ])
            
            return response
            
        except Exception as e:
            logger.error(f"Error exporting bookings: {str(e)}", exc_info=True)
            return Response(
                {'error': 'Failed to export booking data'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )