import uuid
from django.forms import ValidationError # Keep this
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.utils import timezone
from django.db import transaction
from decimal import Decimal
import stripe
from django.conf import settings
from ..models import CustomUser, Booking, Payment, ScheduleInstance
from ..serializers import BookingCreateSerializer, BookingDetailSerializer
from ..utils.email_utils import send_booking_confirmation_email # Adjust path if needed

import logging

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

class PaymentBookingSerializer(BookingCreateSerializer):
    """Extended serializer that skips weekly slot validation for payment processing"""
    # ... (content of this class remains unchanged) ...
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

            # Assuming request.data might not be available here, check for 1 participant
            # Correct check might need context or different approach if participants > 1
            if not instance.can_accommodate(1):
                raise ValidationError(
                    f'Not enough spots available for instance {instance.pk}'
                )

        return value # Return IDs as expected by the serializer


class CreatePaymentIntentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            # Log the incoming request data
            logger.info(f"Received payment intent request: {request.data}")

            # Get the selected slots
            selected_slots = request.data.get('selectedSlots')
            if not selected_slots or not isinstance(selected_slots, list) or len(selected_slots) == 0:
                 logger.warning("CreatePaymentIntentView: selectedSlots missing or invalid.")
                 return Response(
                    {'error': 'selectedSlots is required and must be a non-empty list'},
                    status=status.HTTP_400_BAD_REQUEST
                 )

            serializer = BookingCreateSerializer(
                data={
                    'selectedSlots': selected_slots,
                    'participants': request.data.get('participants', 1),
                    'notes': request.data.get('notes', '')
                },
                context={'request': request} # Pass request context
            )

            if not serializer.is_valid():
                logger.warning(f"CreatePaymentIntentView: BookingCreateSerializer invalid: {serializer.errors}")
                return Response(
                    {'error': serializer.errors},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Get the first schedule instance from the validated context or re-fetch
            # Accessing context added during validation
            validated_instance = serializer.context.get('validated_instance')
            if not validated_instance:
                # Fallback if context wasn't set correctly (shouldn't happen ideally)
                logger.warning("CreatePaymentIntentView: validated_instance not found in serializer context. Re-fetching.")
                first_slot_id = selected_slots[0].get('id')
                if not first_slot_id:
                    return Response({'error': 'First slot ID is missing.'}, status=status.HTTP_400_BAD_REQUEST)
                try:
                    instance = ScheduleInstance.objects.select_related('schedule__option').get(id=first_slot_id)
                except ScheduleInstance.DoesNotExist:
                    return Response({'error': 'Initial schedule instance not found.'}, status=status.HTTP_404_NOT_FOUND)
            else:
                 instance = validated_instance


            option = instance.schedule.option
            booking_type = option.booking_type

            # Get all instances if this is a course booking (using validated context if available)
            if booking_type == 'Full Course':
                all_instances = serializer.context.get('future_course_instances')
                if not all_instances:
                     # Fallback query if context missing
                     logger.warning("CreatePaymentIntentView: future_course_instances not found in context. Re-querying.")
                     all_instances = ScheduleInstance.objects.filter(
                        schedule=instance.schedule,
                        date__gte=instance.date,
                        status='scheduled' # Only include available ones
                    ).order_by('date')
            else:
                all_instances = [instance]

            if not all_instances:
                 logger.error(f"CreatePaymentIntentView: No valid instances found for booking (Initial ID: {instance.id}, Type: {booking_type}).")
                 return Response({'error': 'No available sessions found for this booking.'}, status=status.HTTP_400_BAD_REQUEST)


            # Calculate total amount for all instances
            participants = serializer.validated_data.get('participants', 1) # Use validated participants
            base_total = sum(
                Decimal(inst.price) * participants # Use Decimal for price calculation
                for inst in all_instances
            )

            # Apply service fee
            # Ensure service fee is read from settings or constants, not hardcoded
            # Example: service_fee_rate = getattr(settings, 'SERVICE_FEE_RATE', Decimal('0.13'))
            service_fee_rate = Decimal('0.13') # Keep as is for now
            service_fee = base_total * service_fee_rate
            total_amount_decimal = base_total + service_fee
            total_amount_cents = int(total_amount_decimal * 100) # Convert to cents for Stripe

            # Prepare metadata
            metadata={
                'user_id': str(request.user.userId),
                'first_slot_id': str(instance.id), # Use the confirmed initial instance ID
                'participants': str(participants),
                'base_total': str(base_total.quantize(Decimal("0.01"))), # Format decimal
                'service_fee': str(service_fee.quantize(Decimal("0.01"))), # Format decimal
                'booking_type': str(booking_type),
                'notes': str(serializer.validated_data.get('notes', '')), # Use validated notes
                'is_course': str(booking_type == 'Full Course'),
                'schedule_id': str(instance.schedule.pk),
                'start_date': str(instance.date), # Ensure string format
            }
            # Add end_date only if it's a course and end_date exists
            if booking_type == 'Full Course' and hasattr(instance.schedule, 'end_date') and instance.schedule.end_date:
                 metadata['end_date'] = str(instance.schedule.end_date)


            # Create payment intent with course info
            intent = stripe.PaymentIntent.create(
                amount=total_amount_cents,
                currency='usd', # Consider making currency dynamic or from settings
                payment_method_types=['card'], # Or fetch allowed types
                metadata=metadata
            )

            logger.info(f"Created Payment Intent {intent.id} for user {request.user.email}")

            return Response({
                'clientSecret': intent.client_secret,
                'amount': float(total_amount_decimal.quantize(Decimal("0.01"))), # Return decimal as float
                'service_fee': float(service_fee.quantize(Decimal("0.01"))),
                'base_total': float(base_total.quantize(Decimal("0.01"))),
                'total_sessions': len(all_instances),
                'booking_type': booking_type
            })

        # Keep existing exception handling
        except ScheduleInstance.DoesNotExist:
            logger.error(f"CreatePaymentIntentView: ScheduleInstance not found (maybe invalid ID in request?). Request Data: {request.data}")
            return Response(
                {'error': 'Invalid schedule instance ID provided.'},
                status=status.HTTP_404_NOT_FOUND
            )
        except ValidationError as ve: # Catch DRF validation errors
             logger.warning(f"CreatePaymentIntentView: Validation Error: {ve.detail}")
             return Response({'error': ve.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Error creating payment intent: {str(e)}", exc_info=True)
            return Response(
                {'error': "An unexpected error occurred while preparing payment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class ProcessBookingWebhook(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
        event = None # Initialize event

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
            logger.info(f"Stripe Webhook Received: ID={event.id}, Type={event.type}")

        except ValueError as e:
            # Invalid payload
            logger.error(f"Webhook Error: Invalid payload. {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except stripe.error.SignatureVerificationError as e:
            # Invalid signature
            logger.error(f"Webhook Error: Invalid signature. {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            # Catch any other exceptions during event construction
            logger.error(f"Webhook Error: Unexpected error constructing event. {e}", exc_info=True)
            return Response(status=status.HTTP_400_BAD_REQUEST)


        # Handle the event
        if event.type == 'payment_intent.succeeded':
            payment_intent = event.data.object # contains a stripe.PaymentIntent
            logger.info(f'Webhook: PaymentIntent {payment_intent.id} succeeded.')
            # Call handler function
            try:
                response_data = self.handle_successful_payment(payment_intent)
                # If handler returns data, use it, otherwise default to 200 OK
                return Response(response_data or {}, status=status.HTTP_200_OK)
            except ValidationError as ve:
                 logger.error(f"Webhook Validation Error handling PI {payment_intent.id}: {ve.detail}")
                 # Return 400 so Stripe might retry if configured, or signal processing issue
                 return Response({'error': ve.detail}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                 logger.error(f"Webhook: Error processing successful PI {payment_intent.id}: {e}", exc_info=True)
                 # Return 500 - internal server error, Stripe might retry
                 return Response({'error': 'Internal server error handling payment.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        elif event.type == 'payment_intent.payment_failed':
            payment_intent = event.data.object
            logger.warning(f'Webhook: PaymentIntent {payment_intent.id} failed.')
            # Optionally: Find related pending booking/payment records and mark as failed
            # payment = Payment.objects.filter(stripe_payment_intent_id=payment_intent.id).first()
            # if payment:
            #     payment.status = 'failed'
            #     payment.failure_message = payment_intent.last_payment_error.message if payment_intent.last_payment_error else 'Unknown reason'
            #     payment.save()
            #     if payment.booking:
            #         payment.booking.status = 'failed' # Or keep pending?
            #         payment.booking.payment_status = 'failed'
            #         payment.booking.save()

        # ... handle other event types (charge.refunded, etc.)

        else:
            logger.debug(f'Webhook: Unhandled event type {event.type}')

        # Acknowledge receipt of unhandled or non-critical events
        return Response(status=status.HTTP_200_OK)


    def handle_successful_payment(self, payment_intent):
        # --- Check if Payment already processed ---
        if Payment.objects.filter(stripe_payment_intent_id=payment_intent.id, status='succeeded').exists():
            logger.warning(f"Webhook: PaymentIntent {payment_intent.id} has already been successfully processed. Skipping.")
            return {"message": "Already processed"} # Indicate success but no action taken

        try:
            with transaction.atomic():
                metadata = payment_intent.metadata
                if not metadata:
                     logger.error(f"Webhook Error: Missing metadata for successful PaymentIntent {payment_intent.id}")
                     raise ValidationError("Payment metadata missing.")

                # --- Safely extract metadata ---
                user_id = metadata.get('user_id')
                first_slot_id = metadata.get('first_slot_id')
                participants_str = metadata.get('participants', '1')
                booking_type = metadata.get('booking_type')
                notes = metadata.get('notes', '')
                is_course = metadata.get('is_course') == 'True'
                schedule_id = metadata.get('schedule_id')
                start_date_str = metadata.get('start_date')
                end_date_str = metadata.get('end_date') # Might be empty

                # --- Validate required metadata ---
                required_meta = ['user_id', 'first_slot_id', 'participants', 'booking_type', 'is_course', 'schedule_id', 'start_date']
                if not all(key in metadata for key in required_meta):
                    missing_keys = [key for key in required_meta if key not in metadata]
                    logger.error(f"Webhook Error: Missing required metadata for PI {payment_intent.id}. Missing: {missing_keys}")
                    raise ValidationError(f"Payment metadata incomplete. Missing: {', '.join(missing_keys)}")

                try:
                    participants = int(participants_str)
                    user = CustomUser.objects.get(userId=int(user_id))
                    initial_instance = ScheduleInstance.objects.select_related('schedule__option').get(id=int(first_slot_id))
                    start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
                except (ValueError, TypeError, CustomUser.DoesNotExist, ScheduleInstance.DoesNotExist) as e:
                    logger.error(f"Webhook Error: Invalid metadata types or object not found for PI {payment_intent.id}. Error: {e}")
                    raise ValidationError(f"Invalid payment metadata or related object not found: {e}")

                # --- Determine instances to book ---
                instances_to_book = []
                if is_course:
                    if not end_date_str: # End date is crucial for courses
                        logger.error(f"Webhook Error: Missing end_date metadata for course booking PI {payment_intent.id}.")
                        raise ValidationError("End date missing for course booking.")
                    try:
                        end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
                    except (ValueError, TypeError):
                        logger.error(f"Webhook Error: Invalid end_date format '{end_date_str}' for PI {payment_intent.id}.")
                        raise ValidationError("Invalid end date format for course booking.")

                    # Query instances for the course
                    instances_to_book = list(ScheduleInstance.objects.filter(
                        schedule_id=int(schedule_id), # Use schedule_id from metadata
                        date__gte=start_date,
                        date__lte=end_date,
                        status='scheduled' # Re-check status just in case
                    ).order_by('date'))

                    if not instances_to_book:
                        logger.error(f"Webhook Error: No scheduled instances found for course (Schedule ID: {schedule_id}) between {start_date} and {end_date} for PI {payment_intent.id}.")
                        raise ValidationError("No available sessions found for the specified course range.")
                else:
                    # Single session: Ensure the initial instance is still valid
                    if initial_instance.status != 'scheduled' or initial_instance.date < timezone.now().date():
                        logger.error(f"Webhook Error: Initial instance {initial_instance.id} is no longer available for booking PI {payment_intent.id}.")
                        raise ValidationError("The selected session is no longer available.")
                    instances_to_book = [initial_instance]

                # --- Check Availability Again (Crucial!) ---
                for instance in instances_to_book:
                    if not instance.can_accommodate(participants):
                         logger.error(f"Webhook Error: Instance {instance.id} cannot accommodate {participants} participants for PI {payment_intent.id}. Booking cannot be completed.")
                         # What to do here? Ideally, refund should be triggered.
                         # For now, raise validation error to stop processing.
                         # TODO: Implement automatic refund logic here if needed.
                         raise ValidationError(f"Session on {instance.date} is full. Booking cannot be completed.")


                # --- Create Bookings ---
                booking_group_id = uuid.uuid4() if len(instances_to_book) > 1 else None
                total_amount_decimal = Decimal(payment_intent.amount) / 100
                num_instances = len(instances_to_book)
                # Calculate amount per booking carefully, handle potential division by zero
                amount_per_booking = (total_amount_decimal / num_instances) if num_instances > 0 else Decimal('0.00')

                created_bookings = []
                for instance in instances_to_book:
                    booking = Booking.objects.create(
                        schedule_instance=instance,
                        user=user,
                        booking_group_id=booking_group_id,
                        participants=participants,
                        notes=notes,
                        amount_paid=amount_per_booking.quantize(Decimal("0.01")), # Ensure 2 decimal places
                        status='confirmed', # Mark as confirmed immediately
                        payment_status='paid',
                        enrollment_type=booking_type
                    )
                    created_bookings.append(booking)
                logger.info(f"Webhook: Created {len(created_bookings)} booking(s) for PI {payment_intent.id}. Group ID: {booking_group_id}")

                # --- Create Payment Record ---
                charge = None
                if payment_intent.latest_charge:
                    try:
                        charge = stripe.Charge.retrieve(payment_intent.latest_charge)
                    except stripe.InvalidRequestError as e:
                        logger.warning(f"Webhook: Could not retrieve charge {payment_intent.latest_charge} for PI {payment_intent.id}: {e}")


                payment = Payment.objects.create(
                    # Link payment only to the first booking for simplicity in lookups
                    # Metadata stores all related booking IDs
                    booking=created_bookings[0],
                    stripe_payment_intent_id=payment_intent.id,
                    stripe_charge_id=payment_intent.latest_charge,
                    amount=total_amount_decimal,
                    service_fee_amount=Decimal(metadata.get('service_fee', '0.00')),
                    currency=payment_intent.currency.upper(),
                    status='succeeded', # Mark as succeeded
                    payment_method_type=payment_intent.payment_method_types[0] if payment_intent.payment_method_types else 'card',
                    metadata={ # Store useful info for reconciliation/debugging
                        'booking_group_id': str(booking_group_id) if booking_group_id else None,
                        'booking_ids': [b.id for b in created_bookings],
                        'original_stripe_metadata': metadata # Keep original meta if needed
                    }
                )

                # Add charge details if retrieved
                if charge:
                    payment_method_details = charge.payment_method_details
                    if payment_method_details and payment_method_details.type == 'card':
                        card = payment_method_details.card
                        if card:
                            payment.card_brand = card.brand
                            payment.card_last4 = card.last4
                            payment.card_exp_month = card.exp_month
                            payment.card_exp_year = card.exp_year

                    payment.receipt_url = charge.receipt_url
                    payment.receipt_number = charge.receipt_number
                    payment.billing_details = charge.billing_details.to_dict() if charge.billing_details else {}
                    payment.save() # Save updated card/receipt details

                logger.info(f"Webhook: Created Payment record {payment.id} for PI {payment_intent.id}")

                # --- Send Confirmation Email (using the utility function) ---
                try:
                    # Send confirmation based on the first booking instance created
                    if created_bookings:
                        send_booking_confirmation_email(user, created_bookings[0])
                        logger.info(f"Webhook: Booking confirmation email prepared/queued for booking {created_bookings[0].id}, user {user.email}")
                except Exception as email_error:
                    # Log email error but don't fail the whole webhook processing
                    logger.error(f"Webhook: Failed to send confirmation email for booking {created_bookings[0].id} (PI: {payment_intent.id}): {email_error}", exc_info=True)


                # Return success, maybe include first booking ID
                return {'booking_id': created_bookings[0].id}

        except (ValidationError, ScheduleInstance.DoesNotExist, CustomUser.DoesNotExist) as e:
            # Catch specific errors we raised or expect
            logger.error(f"Webhook Error processing PI {payment_intent.id}: {e}")
            # Re-raise validation errors to potentially return 400
            raise e
        except Exception as e:
            # Catch unexpected errors during the atomic block
            logger.error(f"Webhook CRITICAL Error processing PI {payment_intent.id}: {e}", exc_info=True)
            # Re-raise to signal failure (will result in 500 if not caught higher up)
            raise e