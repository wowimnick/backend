# Create this in a new file: quickstart/views/admin/admin_payment_views.py

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.db import transaction
from django.db.models import Sum, Count, Avg
from datetime import timedelta
from decimal import Decimal

from ...models import Payment, Booking
from ...serializers.booking_management.payment_serializers import AdminPaymentSerializer, AdminBookingPaymentSerializer
from ...utils.permissions import IsAdminUser, check_user_role

import stripe
from django.conf import settings
import logging
import csv
from django.http import HttpResponse

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY

class AdminPaymentViewSet(viewsets.ModelViewSet):
    """Admin-only viewset for managing payments"""
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminPaymentSerializer
    http_method_names = ['get', 'post', 'patch', 'delete']
    
    def get_queryset(self):
        queryset = Payment.objects.select_related(
            'booking__user',
            'booking__schedule_instance__schedule__option__classId__businessId'
        ).all()
        
        # Apply filters
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
            
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                # Search by payment ID
                stripe_payment_intent_id__icontains=search
            ) | Payment.objects.filter(
                # Search by customer name/email
                booking__user__first_name__icontains=search
            ) | Payment.objects.filter(
                booking__user__last_name__icontains=search
            ) | Payment.objects.filter(
                booking__user__email__icontains=search
            )
            
        payment_method = self.request.query_params.get('payment_method')
        if payment_method:
            queryset = queryset.filter(payment_method_type=payment_method)
            
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date and end_date:
            queryset = queryset.filter(created_at__range=[start_date, end_date])
            
        # Apply ordering
        ordering = self.request.query_params.get('ordering', '-created_at')
        return queryset.order_by(ordering)
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get payment statistics for admin dashboard"""
        try:
            # Get date range for statistics (default to last 30 days)
            end_date = timezone.now()
            start_date = end_date - timedelta(days=30)
            
            # Get all payments in this period
            payments = Payment.objects.filter(created_at__range=[start_date, end_date])
            
            # Calculate basic stats
            total_revenue = payments.filter(status='succeeded').aggregate(
                total=Sum('amount')
            )['total'] or 0
            
            # Compare to previous period to get growth
            previous_start = start_date - timedelta(days=30)
            previous_end = end_date - timedelta(days=30)
            
            previous_revenue = Payment.objects.filter(
                created_at__range=[previous_start, previous_end],
                status='succeeded'
            ).aggregate(
                total=Sum('amount')
            )['total'] or 0
            
            # Calculate growth percentage
            revenue_growth = 0
            if previous_revenue > 0:
                revenue_growth = ((total_revenue - previous_revenue) / previous_revenue) * 100
                
            # Get count of pending payments
            pending_payments = Payment.objects.filter(status='pending').count()
            
            # Get total refunded amount
            refunded_amount = payments.filter(
                status__in=['refunded', 'partially_refunded']
            ).aggregate(
                total=Sum('refunded_amount')
            )['total'] or 0
            
            # Return stats
            return Response({
                'total_revenue': total_revenue,
                'revenue_growth': round(revenue_growth, 1),
                'pending_payments': pending_payments,
                'refunded_amount': refunded_amount
            })
        except Exception as e:
            logger.error(f"Error getting payment stats: {str(e)}", exc_info=True)
            return Response(
                {'error': 'Failed to retrieve payment statistics'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'])
    def refund(self, request):
        """Process refund through Stripe (list action)"""
        try:
            payment_intent_id = request.data.get('payment_intent_id')
            amount = request.data.get('amount')
            reason = request.data.get('reason', 'requested_by_customer')

            if not payment_intent_id:
                return Response(
                    {'error': 'payment_intent_id is required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Get payment record
            try:
                payment = Payment.objects.get(stripe_payment_intent_id=payment_intent_id)
            except Payment.DoesNotExist:
                return Response(
                    {'error': 'Payment not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Validate payment status
            if payment.status not in ['succeeded', 'partially_refunded']:
                return Response(
                    {'error': f'Payment with status {payment.status} cannot be refunded'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Calculate refund amount
            available_refund = payment.amount - payment.refunded_amount

            if amount and Decimal(amount) > available_refund:
                return Response(
                    {'error': f'Refund amount exceeds available amount ({available_refund})'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Process refund through Stripe
            refund_amount = int(Decimal(amount) * 100) if amount else None  # Convert to cents

            try:
                refund = stripe.Refund.create(
                    payment_intent=payment_intent_id,
                    amount=refund_amount or 0,
                    reason=reason
                )
            except stripe.StripeError as e:
                logger.error(f"Stripe error: {str(e)}")
                return Response(
                    {'error': str(e)},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Update local payment record
            with transaction.atomic():
                # Calculate new refunded amount
                new_refunded_amount = payment.refunded_amount
                if refund_amount:
                    new_refunded_amount += Decimal(refund_amount) / Decimal(100)  # Convert back from cents
                else:
                    new_refunded_amount = payment.amount

                # Determine new status
                new_status = 'refunded' if new_refunded_amount >= payment.amount else 'partially_refunded'

                # Update payment
                payment.refunded_amount = new_refunded_amount
                payment.status = new_status
                payment.refund_date = timezone.now()
                payment.refund_reason = reason
                payment.save()

                # Update booking if this is a full refund
                if new_status == 'refunded' and payment.booking:
                    payment.booking.payment_status = 'refunded'
                    payment.booking.save(update_fields=['payment_status'])

            # Return success response
            return Response({
                'success': True,
                'refund_id': refund.id,
                'payment_id': payment.id,
                'amount': float(amount) if amount else float(payment.amount),
                'status': new_status
            })

        except Exception as e:
            logger.error(f"Error processing refund: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def mark_paid(self, request, pk=None):
        """Mark a pending payment as paid"""
        payment = self.get_object()
        
        if payment.status != 'pending':
            return Response(
                {'error': 'Only pending payments can be marked as paid'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        payment.status = 'succeeded'
        payment.save()
        
        # Update related booking
        if payment.booking:
            payment.booking.payment_status = 'paid'
            payment.booking.save(update_fields=['payment_status'])
            
        return Response(self.get_serializer(payment).data)
    
    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        """Get payment history/timeline events"""
        payment = self.get_object()
        
        # In a real implementation, you'd fetch real history data
        # Here we'll create simulated history based on the payment's state
        history = [
            {
                'id': 1,
                'action': "Payment Intent Created",
                'date': payment.created_at,
                'user': "System",
                'details': f"Payment intent {payment.stripe_payment_intent_id} created"
            }
        ]
        
        # Add events based on status
        if payment.status != 'pending':
            # Add succeeded or failed event
            if payment.status == 'failed':
                history.append({
                    'id': 2,
                    'action': "Payment Failed",
                    'date': payment.updated_at,
                    'user': "Stripe",
                    'details': f"Payment failed: {payment.failure_message or 'Unknown error'}"
                })
            else:
                # Add success event for all other statuses
                success_time = payment.created_at + timedelta(minutes=2)
                history.append({
                    'id': 2,
                    'action': "Payment Succeeded",
                    'date': success_time,
                    'user': "Stripe",
                    'details': f"Payment of {payment.amount} {payment.currency} successfully processed"
                })
                
                # Add receipt generation if present
                if payment.receipt_url:
                    receipt_time = success_time + timedelta(minutes=1)
                    history.append({
                        'id': 3,
                        'action': "Receipt Generated",
                        'date': receipt_time,
                        'user': "Stripe",
                        'details': f"Receipt {payment.receipt_number or ''} generated and sent to customer"
                    })
        
        # Add refund events
        if payment.status in ['refunded', 'partially_refunded'] and payment.refund_date:
            history.append({
                'id': 4,
                'action': "Refund Processed",
                'date': payment.refund_date,
                'user': "Administrator",
                'details': f"Refund of {payment.refunded_amount} {payment.currency} processed" + 
                          (f" (Reason: {payment.refund_reason})" if payment.refund_reason else "")
            })
            
        # Sort by date
        history.sort(key=lambda x: x['date'])
        
        return Response(history)
    
    @action(detail=True, methods=['get'])
    def receipt(self, request, pk=None):
        """Get payment receipt"""
        payment = self.get_object()
        
        if not payment.receipt_url:
            return Response(
                {'error': 'No receipt available for this payment'},
                status=status.HTTP_404_NOT_FOUND
            )
            
        # In a real implementation, you might generate a PDF here
        # For this example, we'll just return the receipt URL
        return Response({'receipt_url': payment.receipt_url})
    
    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export payments data to CSV"""
        try:
            # Get filtered queryset
            queryset = self.get_queryset()
            
            # Create CSV response
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="payment_data.csv"'
            
            # Create CSV writer
            writer = csv.writer(response)
            
            # Write header
            writer.writerow([
                'ID', 'Transaction ID', 'Amount', 'Currency', 'Status',
                'Payment Method', 'Card Details', 'Date', 'Customer Name',
                'Customer Email', 'Booking ID', 'Business', 'Class'
            ])
            
            # Write data
            for payment in queryset:
                card_details = ""
                if payment.payment_method_type == 'card' and payment.card_brand and payment.card_last4:
                    card_details = f"{payment.card_brand} ending in {payment.card_last4}"
                
                customer_name = ""
                customer_email = ""
                booking_id = ""
                business_name = ""
                class_name = ""
                
                if payment.booking:
                    booking_id = payment.booking.id
                    if payment.booking.user:
                        user = payment.booking.user
                        customer_name = f"{user.first_name} {user.last_name}".strip()
                        customer_email = user.email
                    
                    if payment.booking.schedule_instance:
                        instance = payment.booking.schedule_instance
                        if instance.schedule.option.classId:
                            class_obj = instance.schedule.option.classId
                            class_name = class_obj.title
                            
                            if class_obj.businessId:
                                business_name = class_obj.businessId.businessName
                
                writer.writerow([
                    payment.id,
                    payment.stripe_payment_intent_id,
                    payment.amount,
                    payment.currency,
                    payment.status,
                    payment.payment_method_type,
                    card_details,
                    payment.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                    customer_name,
                    customer_email,
                    booking_id,
                    business_name,
                    class_name
                ])
            
            return response
            
        except Exception as e:
            logger.error(f"Error exporting payments: {str(e)}", exc_info=True)
            return Response(
                {'error': 'Failed to export payment data'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )