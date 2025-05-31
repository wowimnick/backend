# quickstart/payments/views.py (or your actual path)
import uuid
import json # <--- IMPORT JSON MODULE
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.utils import timezone
from django.db import transaction
from decimal import Decimal
import stripe
from django.conf import settings
from ..models import CustomUser, Booking, Payment, ScheduleInstance # Adjust import path if needed
from ..serializers.public.public_booking_serializers import BookingCreateSerializer # Correct import path

from ..utils.email_utils import send_booking_confirmation_email, send_business_new_booking_email

import logging

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

class CreatePaymentIntentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            logger.info(f"CreatePaymentIntentView - Received payment intent request: {request.data}")

            selected_slots = request.data.get('selectedSlots')
            if not selected_slots or not isinstance(selected_slots, list) or len(selected_slots) == 0:
                 logger.warning("CreatePaymentIntentView: selectedSlots missing or invalid.")
                 return Response(
                    {'error': 'selectedSlots is required and must be a non-empty list'},
                    status=status.HTTP_400_BAD_REQUEST
                 )

            serializer_data = {
                'selectedSlots': selected_slots,
                'participants': request.data.get('participants', 1),
                'notes': request.data.get('notes', ''),
                'participant_details': request.data.get('participant_details', [])
            }
            logger.debug(f"CreatePaymentIntentView - Data for BookingCreateSerializer: {serializer_data}")

            serializer = BookingCreateSerializer(
                data=serializer_data,
                context={'request': request}
            )

            if not serializer.is_valid():
                logger.warning(f"CreatePaymentIntentView: BookingCreateSerializer invalid: {serializer.errors}")
                return Response(
                    {'error': serializer.errors},
                    status=status.HTTP_400_BAD_REQUEST
                )

            validated_instance = serializer.context.get('validated_instance')
            if not validated_instance:
                logger.error("CreatePaymentIntentView: validated_instance not found in serializer context.")
                return Response({'error': 'Internal error: Validated instance not found after serialization.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            instance = validated_instance
            option = instance.schedule.option
            booking_type = option.booking_type

            if booking_type == 'Full Course':
                all_instances = serializer.context.get('future_course_instances')
                if not all_instances:
                     logger.error("CreatePaymentIntentView: future_course_instances not found in context for a course.")
                     return Response({'error': 'Internal error: Course instances not found after serialization.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            else:
                all_instances = [instance]

            if not all_instances:
                 logger.error(f"CreatePaymentIntentView: No valid instances found for booking (Initial ID: {instance.id}, Type: {booking_type}).")
                 return Response({'error': 'No available sessions found for this booking.'}, status=status.HTTP_400_BAD_REQUEST)

            participants = serializer.validated_data['participants']
            
            # This base_total is the sum of (slot_price * participants) for all selected instances.
            # This is now considered the total amount the user pays.
            base_total_paid_by_user = sum(
                Decimal(inst.price) * participants
                for inst in all_instances
            )

            # Calculate service fee based on the total amount paid by the user.
            # This fee is for internal records; Stripe Connect handles the actual splits.
            service_fee_rate = getattr(settings, 'SERVICE_FEE_RATE', Decimal('0.13')) # 13%
            service_fee_for_records = base_total_paid_by_user * service_fee_rate
            
            # Amount to be charged by Stripe is the total amount paid by the user.
            total_amount_for_stripe_decimal = base_total_paid_by_user
            total_amount_for_stripe_cents = int(total_amount_for_stripe_decimal * 100)

            participant_details_json_list = serializer.validated_data.get('participant_details', [])
            # Serialize participant_details list to a JSON string for Stripe metadata
            try:
                participant_details_metadata_str = json.dumps(participant_details_json_list)
            except TypeError:
                logger.error(f"CreatePaymentIntentView - Failed to serialize participant_details for Stripe metadata. Data: {participant_details_json_list}")
                participant_details_metadata_str = "[]" # Default to empty JSON array string

            metadata={
                'user_id': str(request.user.userId),
                'first_slot_id': str(instance.id),
                'participants': str(participants),
                'participant_details': participant_details_metadata_str, # Store JSON string
                'gross_amount_paid_by_user': str(base_total_paid_by_user.quantize(Decimal("0.01"))), # Total user paid
                'calculated_service_fee': str(service_fee_for_records.quantize(Decimal("0.01"))), # Our fee portion
                'booking_type': str(booking_type),
                'notes': str(serializer.validated_data.get('notes', '')),
                'is_course': str(booking_type == 'Full Course'),
                'schedule_id': str(instance.schedule.pk),
                'start_date': str(instance.date),
            }
            if booking_type == 'Full Course' and instance.schedule.end_date:
                 metadata['end_date'] = str(instance.schedule.end_date)

            logger.debug(f"CreatePaymentIntentView - Metadata for Stripe: {metadata}")

            intent = stripe.PaymentIntent.create(
                amount=total_amount_for_stripe_cents, # Send the gross amount user pays
                currency=getattr(settings, 'STRIPE_CURRENCY', 'usd').lower(),
                payment_method_types=['card'],
                metadata=metadata
            )
            logger.info(f"CreatePaymentIntentView - Created Payment Intent {intent.id} for user {request.user.email}")

            return Response({
                'clientSecret': intent.client_secret,
                'amount': float(total_amount_for_stripe_decimal.quantize(Decimal("0.01"))), # Gross amount
                'service_fee': float(service_fee_for_records.quantize(Decimal("0.01"))), # Calculated fee
                'base_total': float(base_total_paid_by_user.quantize(Decimal("0.01"))), # Re-iterate: this is now the gross amount
                'total_sessions': len(all_instances),
                'booking_type': booking_type,
                'booking_id': None # Booking not created yet, happens in webhook
            })

        except ScheduleInstance.DoesNotExist:
            logger.error(f"CreatePaymentIntentView: ScheduleInstance not found. Request Data: {request.data}", exc_info=True)
            return Response(
                {'error': 'Invalid schedule instance ID provided.'},
                status=status.HTTP_404_NOT_FOUND
            )
        except DRFValidationError as ve:
             logger.warning(f"CreatePaymentIntentView: DRF Validation Error: {ve.detail}")
             return Response({'error': ve.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"CreatePaymentIntentView - Error creating payment intent: {str(e)}", exc_info=True)
            return Response(
                {'error': "An unexpected error occurred while preparing payment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
class ProcessBookingWebhook(APIView):
    authentication_classes = [] # Webhooks are not authenticated by session/token
    permission_classes = []

    def _attempt_stripe_refund(self, payment_intent_id, reason_message=""):
        """Attempts to refund a Stripe PaymentIntent."""
        try:
            # Check if a Payment record exists and if it's already refunded
            existing_payment_record = Payment.objects.filter(stripe_payment_intent_id=payment_intent_id).first()
            if existing_payment_record and existing_payment_record.status == 'refunded':
                logger.info(f"Refund for PI {payment_intent_id} skipped, already marked as refunded in DB.")
                return True # Indicate already handled

            stripe.Refund.create(payment_intent=payment_intent_id)
            logger.info(f"Stripe refund successfully initiated for PaymentIntent {payment_intent_id}. Reason: {reason_message}")
            
            # Update local Payment record if it exists
            if existing_payment_record:
                existing_payment_record.status = 'refunded'
                existing_payment_record.refund_reason = reason_message or "Refund due to booking finalization error."
                existing_payment_record.refund_date = timezone.now()
                existing_payment_record.save(update_fields=['status', 'refund_reason', 'refund_date'])
            return True
        except stripe.error.InvalidRequestError as ire:
            if "has already been refunded" in str(ire).lower():
                logger.warning(f"Attempted to refund PI {payment_intent_id}, but it was already refunded on Stripe: {ire}")
                if existing_payment_record and existing_payment_record.status != 'refunded': # Sync local DB
                    existing_payment_record.status = 'refunded'
                    existing_payment_record.refund_reason = reason_message or "Already refunded on Stripe, syncing local DB."
                    existing_payment_record.refund_date = timezone.now()
                    existing_payment_record.save(update_fields=['status', 'refund_reason', 'refund_date'])
                return True # Already refunded
            logger.error(f"CRITICAL: Stripe InvalidRequestError while trying to refund PI {payment_intent_id}: {ire}")
            # Potentially send alert to admin for manual check
            return False
        except stripe.StripeError as refund_err:
            logger.error(f"CRITICAL: Failed to refund PaymentIntent {payment_intent_id}: {refund_err}")
            # Potentially send an alert to admins for manual intervention
            return False

    def post(self, request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
        event = None

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
            logger.info(f"ProcessBookingWebhook - Stripe Webhook Received: ID={event.id}, Type={event.type}")
        except ValueError as e: # Invalid payload
            logger.error(f"ProcessBookingWebhook - Webhook Error: Invalid payload. {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except stripe.error.SignatureVerificationError as e: # Invalid signature
            logger.error(f"ProcessBookingWebhook - Webhook Error: Invalid signature. {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"ProcessBookingWebhook - Webhook Error: Unexpected error constructing event. {e}", exc_info=True)
            return Response(status=status.HTTP_400_BAD_REQUEST)

        if event.type == 'payment_intent.succeeded':
            payment_intent = event.data.object
            logger.info(f'ProcessBookingWebhook - PaymentIntent {payment_intent.id} succeeded.')
            try:
                response_data = self.handle_successful_payment(payment_intent)
                return Response(response_data or {}, status=status.HTTP_200_OK)
            except DRFValidationError as ve:
                 error_detail_msg = ve.detail if hasattr(ve, 'detail') else str(ve)
                 logger.error(f"ProcessBookingWebhook - Validation Error handling PI {payment_intent.id}: {error_detail_msg}")
                 self._attempt_stripe_refund(payment_intent.id, f"Booking validation failed: {error_detail_msg}")
                 return Response({'error': error_detail_msg}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                 logger.error(f"ProcessBookingWebhook - Unexpected Error processing successful PI {payment_intent.id}: {e}", exc_info=True)
                 self._attempt_stripe_refund(payment_intent.id, f"Unexpected server error: {e}")
                 return Response({'error': 'Internal server error handling payment. Your payment, if processed, will be refunded.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        elif event.type == 'payment_intent.payment_failed':
            payment_intent = event.data.object
            logger.warning(f'ProcessBookingWebhook - PaymentIntent {payment_intent.id} failed. Reason: {payment_intent.last_payment_error.message if payment_intent.last_payment_error else "Unknown"}')
            # Optionally: Update any related pending Payment record in your DB to 'failed'
            Payment.objects.filter(stripe_payment_intent_id=payment_intent.id, status='pending').update(
                status='failed', 
                failure_message=payment_intent.last_payment_error.message if payment_intent.last_payment_error else "Payment failed on Stripe."
            )
        else:
            logger.debug(f'ProcessBookingWebhook - Unhandled event type {event.type}')

        return Response(status=status.HTTP_200_OK) # Acknowledge other events

    def handle_successful_payment(self, payment_intent):
        # Idempotency check: if this payment intent has already resulted in a successful Payment record.
        if Payment.objects.filter(stripe_payment_intent_id=payment_intent.id, status='succeeded').exists():
            payment_record = Payment.objects.get(stripe_payment_intent_id=payment_intent.id, status='succeeded')
            booking = payment_record.booking # Assuming Payment is linked to one primary Booking
            logger.warning(f"ProcessBookingWebhook - PaymentIntent {payment_intent.id} has already been successfully processed. Booking ID: {booking.id}, Ref: {booking.user_facing_reference}. Skipping.")
            return {
                "message": "Already processed",
                "booking_id": booking.id,
                "user_facing_reference": booking.user_facing_reference
            }
        
        created_bookings_for_email = [] # To store bookings for email notifications
        
        # The core logic for creating bookings and payment record is within this transaction.
        # If any DRFValidationError is raised within, it will be caught by the `post` method,
        # which will then attempt a refund.
        with transaction.atomic():
            metadata = payment_intent.metadata
            if not metadata:
                 logger.error(f"ProcessBookingWebhook - Error: Missing metadata for successful PaymentIntent {payment_intent.id}")
                 raise DRFValidationError("Payment metadata missing.")

            # --- Extract and validate metadata ---
            user_id = metadata.get('user_id')
            first_slot_id = metadata.get('first_slot_id')
            participants_str = metadata.get('participants', '1')
            
            participant_details_str = metadata.get('participant_details', '[]')
            try:
                participant_details = json.loads(participant_details_str)
                if not isinstance(participant_details, list):
                     logger.warning(f"ProcessBookingWebhook - participant_details metadata for PI {payment_intent.id} is not a list after parsing: '{participant_details_str}'. Defaulting to empty list.")
                     participant_details = []
            except json.JSONDecodeError:
                logger.error(f"ProcessBookingWebhook - Error: Could not parse participant_details JSON from metadata for PI {payment_intent.id}. Value: '{participant_details_str}'. Defaulting to empty list.")
                participant_details = []

            booking_type = metadata.get('booking_type')
            notes = metadata.get('notes', '')
            is_course = metadata.get('is_course') == 'True'
            schedule_id_from_meta = metadata.get('schedule_id')
            start_date_str = metadata.get('start_date')
            end_date_str = metadata.get('end_date') # For courses

            gross_amount_paid_by_user_str = metadata.get('gross_amount_paid_by_user')
            calculated_service_fee_str = metadata.get('calculated_service_fee')

            required_meta_keys = [
                'user_id', 'first_slot_id', 'participants', 'participant_details',
                'booking_type', 'is_course', 'schedule_id', 'start_date',
                'gross_amount_paid_by_user', 'calculated_service_fee'
            ]
            if not all(key in metadata for key in required_meta_keys):
                missing_keys = [key for key in required_meta_keys if key not in metadata]
                logger.error(f"ProcessBookingWebhook - Error: Missing required metadata for PI {payment_intent.id}. Missing: {missing_keys}")
                raise DRFValidationError(f"Payment metadata incomplete. Missing: {', '.join(missing_keys)}")

            try:
                participants = int(participants_str)
                user = CustomUser.objects.get(userId=int(user_id))
                initial_instance = ScheduleInstance.objects.select_related(
                    'schedule__option__classId__businessId__owner' # For notifications
                ).prefetch_related(
                    'schedule__option__classId__businessId__managers' # For notifications
                ).get(id=int(first_slot_id))
                start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
                
                gross_amount_paid_by_user = Decimal(gross_amount_paid_by_user_str)
                calculated_service_fee = Decimal(calculated_service_fee_str)
            except (ValueError, TypeError, CustomUser.DoesNotExist, ScheduleInstance.DoesNotExist) as e:
                logger.error(f"ProcessBookingWebhook - Error: Invalid metadata types or object not found for PI {payment_intent.id}. Error: {e}")
                raise DRFValidationError(f"Invalid payment metadata or related object not found: {e}")

            # --- Determine instances to book and check availability ---
            instances_to_book = []
            if is_course:
                if not end_date_str:
                    logger.error(f"ProcessBookingWebhook - Error: Missing end_date metadata for course booking PI {payment_intent.id}.")
                    raise DRFValidationError("End date missing for course booking.")
                try:
                    end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    logger.error(f"ProcessBookingWebhook - Error: Invalid end_date format '{end_date_str}' for PI {payment_intent.id}.")
                    raise DRFValidationError("Invalid end date format for course booking.")

                instances_to_book = list(ScheduleInstance.objects.filter(
                    schedule_id=int(schedule_id_from_meta),
                    date__gte=start_date,
                    date__lte=end_date,
                    status='scheduled' # Ensure we only book scheduled instances
                ).order_by('date'))

                if not instances_to_book:
                    logger.error(f"ProcessBookingWebhook - Error: No scheduled instances found for course (Schedule ID: {schedule_id_from_meta}) between {start_date} and {end_date} for PI {payment_intent.id}.")
                    raise DRFValidationError("No available sessions found for the specified course range. Your payment will be refunded.")
            else: # Single Session
                if initial_instance.status != 'scheduled' or initial_instance.date < timezone.now().date():
                    logger.error(f"ProcessBookingWebhook - Error: Initial instance {initial_instance.id} is no longer available for booking PI {payment_intent.id}.")
                    raise DRFValidationError("The selected session is no longer available. Your payment will be refunded.")
                instances_to_book = [initial_instance]

            for instance_check in instances_to_book:
                # Re-check availability AT THE TIME OF BOOKING
                if not instance_check.can_accommodate(participants):
                     logger.error(f"ProcessBookingWebhook - Error: Instance {instance_check.id} cannot accommodate {participants} participants for PI {payment_intent.id}. Booking cannot be completed.")
                     raise DRFValidationError(f"Unfortunately, the session on {instance_check.date.strftime('%b %d, %Y')} at {instance_check.time.strftime('%I:%M %p')} is now full. Your payment will be refunded.")

            # --- Create Booking(s) ---
            booking_group_id = uuid.uuid4() if len(instances_to_book) > 1 else None
            
            total_amount_from_stripe = Decimal(payment_intent.amount_received) / 100 # Amount Stripe says was paid
            if total_amount_from_stripe != gross_amount_paid_by_user:
                 logger.warning(f"ProcessBookingWebhook - Discrepancy for PI {payment_intent.id}: Stripe amount_received ({total_amount_from_stripe}) vs metadata gross_amount_paid_by_user ({gross_amount_paid_by_user}). Using Stripe's value for accounting.")
            
            num_instances = len(instances_to_book)
            amount_per_booking_instance = (total_amount_from_stripe / num_instances) if num_instances > 0 else Decimal('0.00')

            # Temp store for email
            
            for current_sch_instance in instances_to_book:
                booking = Booking( # user_facing_reference generated by Booking.save()
                    schedule_instance=current_sch_instance,
                    user=user,
                    booking_group_id=booking_group_id,
                    participants=participants,
                    participant_details=participant_details,
                    notes=notes,
                    amount_paid=amount_per_booking_instance.quantize(Decimal("0.01")),
                    status='confirmed', # Booking is confirmed now
                    payment_status='paid',
                    enrollment_type=booking_type
                )
                booking.save() # This triggers _generate_user_facing_reference
                created_bookings_for_email.append(booking)
            
            logger.info(f"ProcessBookingWebhook - Created {len(created_bookings_for_email)} booking(s) for PI {payment_intent.id}. Group ID: {booking_group_id}. First ref: {created_bookings_for_email[0].user_facing_reference if created_bookings_for_email else 'N/A'}")

            # --- Create Payment Record ---
            charge_details = None
            if payment_intent.latest_charge: # latest_charge can be null if PI was confirmed without a charge (e.g. SEPA direct debit)
                try:
                    charge_details = stripe.Charge.retrieve(payment_intent.latest_charge)
                except stripe.StripeError as e:
                    logger.warning(f"ProcessBookingWebhook - Could not retrieve charge {payment_intent.latest_charge} for PI {payment_intent.id}: {e}")

            payment_record = Payment.objects.create(
                booking=created_bookings_for_email[0], # Link payment to the first booking in the series/group
                stripe_payment_intent_id=payment_intent.id,
                stripe_charge_id=payment_intent.latest_charge,
                amount=total_amount_from_stripe, 
                service_fee_amount=calculated_service_fee, 
                currency=payment_intent.currency.upper(),
                status='succeeded',
                payment_method_type=payment_intent.payment_method_types[0] if payment_intent.payment_method_types else 'card',
                metadata={
                    'booking_group_id': str(booking_group_id) if booking_group_id else None,
                    'booking_ids': [b.id for b in created_bookings_for_email],
                    'user_facing_references': [b.user_facing_reference for b in created_bookings_for_email if b.user_facing_reference],
                    'original_stripe_metadata': metadata # Store original metadata for auditing
                }
            )

            if charge_details:
                payment_method_details = charge_details.payment_method_details
                if payment_method_details and payment_method_details.type == 'card':
                    card = payment_method_details.card
                    if card:
                        payment_record.card_brand = card.brand
                        payment_record.card_last4 = card.last4
                        payment_record.card_exp_month = card.exp_month
                        payment_record.card_exp_year = card.exp_year
                payment_record.receipt_url = charge_details.receipt_url
                payment_record.receipt_number = charge_details.receipt_number # May not always be present
                payment_record.billing_details = charge_details.billing_details.to_dict() if charge_details.billing_details else {}
                payment_record.save()
            
            logger.info(f"ProcessBookingWebhook - Created Payment record {payment_record.id} for PI {payment_intent.id}")
            
            # End of atomic transaction. If we reach here, DB operations were successful.
            # Email sending will happen outside.

        # --- Email Sending (AFTER successful transaction.atomic block) ---
        if created_bookings_for_email:
            first_booking = created_bookings_for_email[0]
            try:
                send_booking_confirmation_email(user, first_booking) # Send for the first booking in a series
                logger.info(f"ProcessBookingWebhook - Booking confirmation email prepared/queued for booking {first_booking.id} (Ref: {first_booking.user_facing_reference}), user {user.email}")
            except Exception as email_error:
                logger.error(f"ProcessBookingWebhook - Failed to send confirmation email for booking {first_booking.id} (PI: {payment_intent.id}): {email_error}", exc_info=True)

            try:
                business_info_obj = initial_instance.schedule.option.classId.businessId
                if business_info_obj and business_info_obj.newBookingNotification:
                    # Notify owner and managers
                    recipients_to_notify = set()
                    if business_info_obj.owner and business_info_obj.owner.email:
                        recipients_to_notify.add(business_info_obj.owner)
                    for manager in business_info_obj.managers.all():
                        if manager and manager.email:
                            recipients_to_notify.add(manager)
                    
                    for biz_user in recipients_to_notify:
                        send_business_new_booking_email(biz_user, first_booking)
                        logger.info(f"ProcessBookingWebhook - New Booking notification email prepared/queued for booking {first_booking.id} to business user {biz_user.email}")
                else:
                     logger.info(f"ProcessBookingWebhook - Skipped new booking notification for business {business_info_obj.businessId if business_info_obj else 'N/A'} (Setting disabled or missing owner/managers)")
            except AttributeError as ae_email: # Catch errors accessing related objects for email
                 logger.error(f"ProcessBookingWebhook - Error accessing business details for notification email for booking {first_booking.id} (PI: {payment_intent.id}): {ae_email}", exc_info=True)
            except Exception as email_error_biz: # Catch general email sending errors
                 logger.error(f"ProcessBookingWebhook - Failed to send new booking notification email for booking {first_booking.id} (PI: {payment_intent.id}): {email_error_biz}", exc_info=True)
            
            return {
                'booking_id': first_booking.id,
                'user_facing_reference': first_booking.user_facing_reference
            }
        else: # Should ideally not be reached if transaction succeeded and bookings were expected
            logger.error(f"ProcessBookingWebhook - Transaction successful but no bookings were created/retrieved for PI {payment_intent.id}")
            raise DRFValidationError("Booking creation failed unexpectedly after payment. Your payment will be refunded.")