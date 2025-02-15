import uuid
from django.forms import ValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.utils import timezone
from django.db import transaction
from decimal import Decimal
import stripe
from django.conf import settings
from ..models import CustomUser, Booking, ScheduleInstance
from ..serializers import BookingCreateSerializer, BookingDetailSerializer

import logging

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

class PaymentBookingSerializer(BookingCreateSerializer):
    """Extended serializer that skips weekly slot validation for payment processing"""
    def validate_schedule_instances(self, value):
        # Keep working with IDs as expected by the parent serializer
        instances = ScheduleInstance.objects.select_related(
            'schedule__option'
        ).filter(id__in=value)
        
        if len(instances) != len(value):
            raise ValidationError('One or more schedule instances not found')

        # Check each instance's availability
        for instance in instances:
            if instance.status != 'scheduled':
                raise ValidationError(
                    f'Instance {instance.pk} is not available for booking'
                )
            
            if instance.date < timezone.now().date():
                raise ValidationError(
                    f'Cannot book past instance {instance.pk}'
                )
            
            if not instance.can_accommodate(1):
                raise ValidationError(
                    f'Not enough spots available for instance {instance.pk}'
                )
        
        return value  # Return IDs as expected by the serializer


class CreatePaymentIntentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            # Log the incoming request data
            logger.info(f"Received payment intent request: {request.data}")
            
            # Get the selected slots
            selected_slots = request.data.get('selectedSlots')
            if not selected_slots or not isinstance(selected_slots, list) or len(selected_slots) == 0:
                return Response(
                    {'error': 'selectedSlots is required and must not be empty'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Validate the booking data with new structure
            serializer = BookingCreateSerializer(data={
                'selectedSlots': selected_slots,
                'participants': request.data.get('participants', 1),
                'notes': request.data.get('notes', '')
            })
            
            if not serializer.is_valid():
                return Response(
                    {'error': serializer.errors},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Get the first schedule instance
            first_slot = selected_slots[0]
            instance = ScheduleInstance.objects.select_related(
                'schedule__option'
            ).get(id=first_slot['id'])
            
            option = instance.schedule.option
            booking_type = option.booking_type
            
            # Get all instances if this is a course booking
            all_instances = []
            if booking_type == 'Full Course':
                all_instances = ScheduleInstance.objects.filter(
                    schedule=instance.schedule,
                    date__gte=instance.date,
                    date__lte=instance.schedule.end_date,
                    status='scheduled'
                ).order_by('date')
            else:
                all_instances = [instance]
            
            # Calculate total amount for all instances
            participants = request.data.get('participants', 1)
            base_total = sum(
                float(inst.price) * participants 
                for inst in all_instances
            )
            
            # Apply service fee
            service_fee_rate = Decimal('0.13')
            service_fee = Decimal(str(base_total)) * service_fee_rate
            total_amount = int((Decimal(str(base_total)) + service_fee) * 100)

            # Create payment intent with course info
            intent = stripe.PaymentIntent.create(
                amount=total_amount,
                currency='usd',
                payment_method_types=['card'],
                metadata={
                    'user_id': str(request.user.userId),
                    'first_slot_id': str(first_slot['id']), 
                    'participants': str(participants),
                    'base_total': str(float(base_total)),
                    'service_fee': str(float(service_fee)),
                    'booking_type': str(booking_type),
                    'notes': str(request.data.get('notes', '')),
                    'is_course': str(booking_type == 'Full Course'),
                    'schedule_id': str(instance.schedule.pk),
                    'start_date': instance.date.isoformat(),
                    'end_date': instance.schedule.end_date.isoformat() if booking_type == 'Full Course' and instance.schedule.end_date else ''
                }
            )

            return Response({
                'clientSecret': intent.client_secret,
                'amount': total_amount / 100,
                'service_fee': float(service_fee),
                'base_total': float(base_total),
                'total_sessions': len(all_instances),
                'booking_type': booking_type
            })

        except ScheduleInstance.DoesNotExist:
            return Response(
                {'error': 'Invalid schedule instance'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error creating payment intent: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class ProcessBookingWebhook(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
        
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
            
            logger.info(f"Processing Stripe event: {event.type}")

            if event.type == 'payment_intent.succeeded':
                return self.handle_successful_payment(event.data.object)
            
            return Response(status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def handle_successful_payment(self, payment_intent):
        try:
            with transaction.atomic():
                metadata = payment_intent.metadata
                booking_type = metadata['booking_type']
                
                # Get initial instance for validation using first_slot_id
                first_slot_id = int(metadata['first_slot_id'])
                initial_instance = ScheduleInstance.objects.select_related(
                    'schedule__option'
                ).get(id=first_slot_id)
                
                # Get all instances to book
                instances_to_book = []
                if metadata.get('is_course') == 'True':
                    instances_to_book = ScheduleInstance.objects.filter(
                        schedule=initial_instance.schedule,
                        date__gte=metadata['start_date'],
                        date__lte=metadata['end_date'],
                        status='scheduled'
                    ).order_by('date')
                else:
                    instances_to_book = [initial_instance]

                # Get user directly
                user = CustomUser.objects.get(userId=metadata['user_id'])

                # Create bookings for all instances
                booking_group_id = uuid.uuid4() if len(instances_to_book) > 1 else None
                amount_per_booking = Decimal(str(metadata['base_total'])) / len(instances_to_book)
                
                bookings = []
                for instance in instances_to_book:
                    booking = Booking.objects.create(
                        schedule_instance=instance,
                        user=user,
                        booking_group_id=booking_group_id,
                        participants=int(metadata['participants']),
                        notes=metadata.get('notes', ''),
                        amount_paid=amount_per_booking,
                        status='confirmed',
                        payment_status='paid',
                        enrollment_type=booking_type
                    )
                    bookings.append(booking)

                # Return first booking as response
                return Response(BookingDetailSerializer(bookings[0]).data)

        except Exception as e:
            logger.error(f"Failed to process payment: {str(e)}", exc_info=True)
            raise ValidationError("Failed to process payment")