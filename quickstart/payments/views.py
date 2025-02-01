import stripe
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from ..models import Booking, ScheduleInstance, ClassOption
from decimal import Decimal

stripe.api_key = settings.STRIPE_SECRET_KEY

class CreatePaymentIntentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            # Extract booking details from request
            schedule_instance_id = request.data.get('schedule_instance')
            participants = request.data.get('participants', 1)

            # Validate schedule instance
            try:
                schedule_instance = ScheduleInstance.objects.get(id=schedule_instance_id)
                class_option = schedule_instance.schedule.option
            except ScheduleInstance.DoesNotExist:
                return Response({
                    'error': 'Invalid schedule instance'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Calculate total amount
            # Convert to cents (Stripe requires amount in smallest currency unit)
            base_price = Decimal(schedule_instance.price)
            service_fee_rate = Decimal('0.13')  # 13% service fee
            
            base_total = base_price * participants
            service_fee = base_total * service_fee_rate
            total_amount = int((base_total + service_fee) * 100)  # Convert to cents

            # Create payment intent
            intent = stripe.PaymentIntent.create(
                amount=total_amount,
                currency='usd',
                payment_method_types=['card'],
                metadata={
                    'schedule_instance_id': schedule_instance_id,
                    'participants': participants,
                    'user_id': request.user.userId,
                    'base_price': float(base_price),
                    'service_fee': float(service_fee)
                }
            )

            return Response({
                'clientSecret': intent.client_secret,
                'amount': total_amount / 100,  # Convert back to dollars for frontend display
                'currency': 'USD',
                'base_price': float(base_price),
                'service_fee': float(service_fee)
            })

        except Exception as e:
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class ConfirmPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            payment_intent_id = request.data.get('payment_intent_id')
            
            # Retrieve the PaymentIntent from Stripe
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            
            # Extract metadata
            metadata = payment_intent.metadata
            schedule_instance_id = metadata.get('schedule_instance_id')
            participants = int(metadata.get('participants', 1))
            base_price = Decimal(metadata.get('base_price'))
            service_fee = Decimal(metadata.get('service_fee'))
            
            # Verify payment was successful
            if payment_intent.status != 'succeeded':
                return Response({
                    'error': 'Payment not completed'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Create booking
            booking = Booking.objects.create(
                schedule_instance_id=schedule_instance_id,
                student=request.user.student_profile,
                participants=participants,
                status='confirmed',
                payment_status='paid',
                amount_paid=base_price * participants + service_fee,
                notes=request.data.get('notes', '')
            )
            
            return Response({
                'booking_id': booking.id,
                'status': 'success',
                'message': 'Booking confirmed and payment processed',
                'payment_intent_id': payment_intent_id,
                'booked_date': booking.booking_date.isoformat()
            })
        
        except Exception as e:
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)