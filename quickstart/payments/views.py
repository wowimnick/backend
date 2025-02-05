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
from ..models import CustomUser, Student, Booking, ScheduleInstance
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
                    f'Instance {instance.id} is not available for booking'
                )
            
            if instance.date < timezone.now().date():
                raise ValidationError(
                    f'Cannot book past instance {instance.id}'
                )
            
            if not instance.can_accommodate(1):
                raise ValidationError(
                    f'Not enough spots available for instance {instance.id}'
                )
        
        return value  # Return IDs as expected by the serializer


class CreatePaymentIntentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            # Use regular BookingCreateSerializer for initial validation
            serializer = BookingCreateSerializer(data={
                'schedule_instances': request.data.get('schedule_instances', [])
            })
            
            if not serializer.is_valid():
                return Response(
                    {'error': serializer.errors},
                    status=status.HTTP_400_BAD_REQUEST
                )

            validated_data = serializer.validated_data
            schedule_instances = validated_data['schedule_instances']
            
            # Get first instance to determine booking type
            first_instance = ScheduleInstance.objects.select_related(
                'schedule__option'
            ).get(id=schedule_instances[0])
            
            option = first_instance.schedule.option
            booking_type = option.booking_type
            
            # Initialize list of all instances to be booked
            all_instances = []
            
            if booking_type == 'Recurring Classes':
                # For recurring bookings, generate future instances
                weeks = 4  # Default to 4 weeks
                if option.recurrence_pattern == 'biweekly':
                    weeks = 8  # 8 weeks for biweekly to get 4 sessions
                
                for instance_id in schedule_instances:
                    instance = ScheduleInstance.objects.get(id=instance_id)
                    schedule = instance.schedule
                    current_date = instance.date
                    
                    # Add initial instance
                    all_instances.append(instance)
                    
                    # Add future instances
                    for week in range(1, weeks):
                        if option.recurrence_pattern == 'biweekly' and week % 2 == 1:
                            continue
                            
                        future_date = current_date + timezone.timedelta(weeks=week)
                        try:
                            future_instance = ScheduleInstance.objects.get(
                                schedule=schedule,
                                date=future_date,
                                time=instance.time
                            )
                        except ScheduleInstance.DoesNotExist:
                            future_instance = ScheduleInstance.objects.create(
                                schedule=schedule,
                                date=future_date,
                                time=instance.time,
                                price=instance.price,
                                max_participants=instance.max_participants,
                                status='scheduled'
                            )
                        all_instances.append(future_instance)
            else:
                # For single sessions, just use the provided instances
                all_instances = [
                    ScheduleInstance.objects.get(id=instance_id)
                    for instance_id in schedule_instances
                ]
            
            # Calculate total amount
            participants = request.data.get('participants', 1)
            base_total = sum(
                float(instance.price) * participants 
                for instance in all_instances
            )
            
            # Apply service fee
            service_fee_rate = Decimal('0.13')  # 13% service fee
            service_fee = Decimal(str(base_total)) * service_fee_rate
            total_amount = int((Decimal(str(base_total)) + service_fee) * 100)  # Convert to cents

            # Create payment intent with complete booking details
            intent = stripe.PaymentIntent.create(
                amount=total_amount,
                currency='usd',
                payment_method_types=['card'],
                metadata={
                    'user_id': request.user.userId,
                    'schedule_instances': ','.join(str(i.id) for i in all_instances),
                    'participants': participants,
                    'base_total': float(base_total),
                    'service_fee': float(service_fee),
                    'booking_type': booking_type,
                    'sessions_per_week': option.sessions_per_week if booking_type == 'Recurring Classes' else None,
                    'recurrence_pattern': option.recurrence_pattern if booking_type == 'Recurring Classes' else None
                }
            )

            return Response({
                'clientSecret': intent.client_secret,
                'amount': total_amount / 100,  # Convert back to dollars
                'currency': 'USD',
                'base_total': float(base_total),
                'service_fee': float(service_fee),
                'total_sessions': len(all_instances),
                'booking_details': {
                    'type': booking_type,
                    'sessions_per_week': option.sessions_per_week if booking_type == 'Recurring Classes' else None,
                    'recurrence_pattern': option.recurrence_pattern if booking_type == 'Recurring Classes' else None
                }
            })

        except Exception as e:
            logger.error(f"Error creating payment intent: {str(e)}", exc_info=True)
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
            logger.info(f"Processing successful payment: {payment_intent.id}")
            
            with transaction.atomic():
                metadata = payment_intent.metadata
                
                # Get instance IDs from metadata
                instance_ids = [
                    int(id) for id in metadata['schedule_instances'].split(',')
                ]
                
                # First validate using the serializer (which works with IDs)
                booking_data = {
                    'schedule_instances': instance_ids
                }

                # Use PaymentBookingSerializer that skips weekly validation
                serializer = PaymentBookingSerializer(data=booking_data)
                if not serializer.is_valid():
                    logger.error(f"Invalid booking data: {serializer.errors}")
                    raise ValidationError(serializer.errors)

                # After validation, fetch all instances for creating bookings
                schedule_instances = ScheduleInstance.objects.select_related(
                    'schedule__option'
                ).filter(id__in=instance_ids)
                
                if len(schedule_instances) != len(instance_ids):
                    logger.error("Some schedule instances not found")
                    raise ValidationError("One or more schedule instances not found")
                
                # Get user and create student profile
                try:
                    user = CustomUser.objects.get(userId=metadata['user_id'])
                    student, _ = Student.objects.get_or_create(
                        user=user,
                        defaults={'enrollment_date': timezone.now().date()}
                    )
                except CustomUser.DoesNotExist:
                    logger.error(f"User not found: {metadata['user_id']}")
                    raise ValidationError("User not found")

                # Create bookings using the actual ScheduleInstance objects
                booking_group_id = uuid.uuid4()
                amount_per_booking = Decimal(str(metadata['base_total'])) / len(schedule_instances)

                bookings = []
                for instance in schedule_instances:
                    booking = Booking.objects.create(
                        schedule_instance=instance,  # Using actual instance object
                        student=student,
                        booking_group_id=booking_group_id,
                        participants=int(metadata['participants']),
                        amount_paid=amount_per_booking,
                        status='confirmed',
                        payment_status='paid',
                        enrollment_type=metadata['booking_type'],
                        sessions_per_week=metadata.get('sessions_per_week'),
                        recurrence_pattern=metadata.get('recurrence_pattern')
                    )
                    bookings.append(booking)
                    logger.info(f"Created booking {booking.id} for instance {instance.id}")

                return Response(BookingDetailSerializer(bookings[0]).data)

        except Exception as e:
            logger.error(f"Failed to process payment: {str(e)}", exc_info=True)
            raise