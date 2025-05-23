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
            base_total = sum(
                Decimal(inst.price) * participants
                for inst in all_instances
            )

            service_fee_rate = getattr(settings, 'SERVICE_FEE_RATE', Decimal('0.13'))
            service_fee = base_total * service_fee_rate
            total_amount_decimal = base_total + service_fee
            total_amount_cents = int(total_amount_decimal * 100)

            participant_details_json_list = serializer.validated_data.get('participant_details', [])
            # --- CORRECTLY SERIALIZE TO JSON STRING FOR STRIPE METADATA ---
            participant_details_metadata_str = json.dumps(participant_details_json_list)

            metadata={
                'user_id': str(request.user.userId),
                'first_slot_id': str(instance.id),
                'participants': str(participants),
                'participant_details': participant_details_metadata_str, # Store proper JSON string
                'base_total': str(base_total.quantize(Decimal("0.01"))),
                'service_fee': str(service_fee.quantize(Decimal("0.01"))),
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
                amount=total_amount_cents,
                currency=getattr(settings, 'STRIPE_CURRENCY', 'usd').lower(),
                payment_method_types=['card'],
                metadata=metadata
            )
            logger.info(f"CreatePaymentIntentView - Created Payment Intent {intent.id} for user {request.user.email}")

            return Response({
                'clientSecret': intent.client_secret,
                'amount': float(total_amount_decimal.quantize(Decimal("0.01"))),
                'service_fee': float(service_fee.quantize(Decimal("0.01"))),
                'base_total': float(base_total.quantize(Decimal("0.01"))),
                'total_sessions': len(all_instances),
                'booking_type': booking_type,
                'booking_id': None
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
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
        event = None

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
            logger.info(f"ProcessBookingWebhook - Stripe Webhook Received: ID={event.id}, Type={event.type}")
        except ValueError as e:
            logger.error(f"ProcessBookingWebhook - Webhook Error: Invalid payload. {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except stripe.SignatureVerificationError as e:
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
                 error_detail = ve.detail if hasattr(ve, 'detail') else ve.args
                 logger.error(f"ProcessBookingWebhook - Validation Error handling PI {payment_intent.id}: {error_detail}")
                 return Response({'error': error_detail}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                 logger.error(f"ProcessBookingWebhook - Error processing successful PI {payment_intent.id}: {e}", exc_info=True)
                 return Response({'error': 'Internal server error handling payment.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        elif event.type == 'payment_intent.payment_failed':
            payment_intent = event.data.object
            logger.warning(f'ProcessBookingWebhook - PaymentIntent {payment_intent.id} failed.')
        else:
            logger.debug(f'ProcessBookingWebhook - Unhandled event type {event.type}')

        return Response(status=status.HTTP_200_OK)


    def handle_successful_payment(self, payment_intent):
        if Payment.objects.filter(stripe_payment_intent_id=payment_intent.id, status='succeeded').exists():
            logger.warning(f"ProcessBookingWebhook - PaymentIntent {payment_intent.id} has already been successfully processed. Skipping.")
            return {"message": "Already processed"}

        try:
            with transaction.atomic():
                metadata = payment_intent.metadata
                if not metadata:
                     logger.error(f"ProcessBookingWebhook - Error: Missing metadata for successful PaymentIntent {payment_intent.id}")
                     raise DRFValidationError("Payment metadata missing.")

                user_id = metadata.get('user_id')
                first_slot_id = metadata.get('first_slot_id')
                participants_str = metadata.get('participants', '1')
                # --- CORRECTLY PARSE PARTICIPANT_DETAILS FROM JSON STRING ---
                participant_details_str = metadata.get('participant_details', '[]')
                try:
                    participant_details = json.loads(participant_details_str)
                    if not isinstance(participant_details, list):
                         logger.warning(f"ProcessBookingWebhook - participant_details metadata for PI {payment_intent.id} is not a list after parsing: {participant_details_str}. Defaulting to empty list.")
                         participant_details = []
                except json.JSONDecodeError:
                    logger.error(f"ProcessBookingWebhook - Error: Could not parse participant_details JSON from metadata for PI {payment_intent.id}. Value: '{participant_details_str}'. Defaulting to empty list.")
                    participant_details = [] # Default to empty list if JSON is malformed

                booking_type = metadata.get('booking_type')
                notes = metadata.get('notes', '')
                is_course = metadata.get('is_course') == 'True'
                schedule_id = metadata.get('schedule_id')
                start_date_str = metadata.get('start_date')
                end_date_str = metadata.get('end_date')

                required_meta = ['user_id', 'first_slot_id', 'participants', 'booking_type', 'is_course', 'schedule_id', 'start_date', 'participant_details']
                if not all(key in metadata for key in required_meta):
                    missing_keys = [key for key in required_meta if key not in metadata]
                    logger.error(f"ProcessBookingWebhook - Error: Missing required metadata for PI {payment_intent.id}. Missing: {missing_keys}")
                    raise DRFValidationError(f"Payment metadata incomplete. Missing: {', '.join(missing_keys)}")

                try:
                    participants = int(participants_str)
                    user = CustomUser.objects.get(userId=int(user_id))
                    initial_instance = ScheduleInstance.objects.select_related(
                        'schedule__option__classId__businessId__owner'
                    ).prefetch_related(
                        'schedule__option__classId__businessId__managers'
                    ).get(id=int(first_slot_id))
                    start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
                except (ValueError, TypeError, CustomUser.DoesNotExist, ScheduleInstance.DoesNotExist) as e:
                    logger.error(f"ProcessBookingWebhook - Error: Invalid metadata types or object not found for PI {payment_intent.id}. Error: {e}")
                    raise DRFValidationError(f"Invalid payment metadata or related object not found: {e}")

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
                        schedule_id=int(schedule_id),
                        date__gte=start_date,
                        date__lte=end_date,
                        status='scheduled'
                    ).order_by('date'))

                    if not instances_to_book:
                        logger.error(f"ProcessBookingWebhook - Error: No scheduled instances found for course (Schedule ID: {schedule_id}) between {start_date} and {end_date} for PI {payment_intent.id}.")
                        raise DRFValidationError("No available sessions found for the specified course range.")
                else:
                    if initial_instance.status != 'scheduled' or initial_instance.date < timezone.now().date():
                        logger.error(f"ProcessBookingWebhook - Error: Initial instance {initial_instance.id} is no longer available for booking PI {payment_intent.id}.")
                        raise DRFValidationError("The selected session is no longer available.")
                    instances_to_book = [initial_instance]

                for instance_check in instances_to_book:
                    if not instance_check.can_accommodate(participants):
                         logger.error(f"ProcessBookingWebhook - Error: Instance {instance_check.id} cannot accommodate {participants} participants for PI {payment_intent.id}. Booking cannot be completed.")
                         raise DRFValidationError(f"Session on {instance_check.date} at {instance_check.time} is full. Booking cannot be completed.")

                booking_group_id = uuid.uuid4() if len(instances_to_book) > 1 else None
                total_amount_decimal = Decimal(payment_intent.amount_received) / 100
                num_instances = len(instances_to_book)
                amount_per_booking = (total_amount_decimal / num_instances) if num_instances > 0 else Decimal('0.00')

                created_bookings = []
                for current_sch_instance in instances_to_book:
                    booking = Booking.objects.create(
                        schedule_instance=current_sch_instance,
                        user=user,
                        booking_group_id=booking_group_id,
                        participants=participants,
                        participant_details=participant_details, # Now this is a Python list of dicts
                        notes=notes,
                        amount_paid=amount_per_booking.quantize(Decimal("0.01")),
                        status='confirmed',
                        payment_status='paid',
                        enrollment_type=booking_type
                    )
                    created_bookings.append(booking)
                logger.info(f"ProcessBookingWebhook - Created {len(created_bookings)} booking(s) for PI {payment_intent.id}. Group ID: {booking_group_id}")

                charge_details = None
                if payment_intent.latest_charge:
                    try:
                        charge_details = stripe.Charge.retrieve(payment_intent.latest_charge)
                    except stripe.StripeError as e:
                        logger.warning(f"ProcessBookingWebhook - Could not retrieve charge {payment_intent.latest_charge} for PI {payment_intent.id}: {e}")

                payment = Payment.objects.create(
                    booking=created_bookings[0],
                    stripe_payment_intent_id=payment_intent.id,
                    stripe_charge_id=payment_intent.latest_charge,
                    amount=total_amount_decimal,
                    service_fee_amount=Decimal(metadata.get('service_fee', '0.00')),
                    currency=payment_intent.currency.upper(),
                    status='succeeded',
                    payment_method_type=payment_intent.payment_method_types[0] if payment_intent.payment_method_types else 'card',
                    metadata={
                        'booking_group_id': str(booking_group_id) if booking_group_id else None,
                        'booking_ids': [b.id for b in created_bookings],
                        'original_stripe_metadata': metadata
                    }
                )

                if charge_details:
                    payment_method_details = charge_details.payment_method_details
                    if payment_method_details and payment_method_details.type == 'card':
                        card = payment_method_details.card
                        if card:
                            payment.card_brand = card.brand
                            payment.card_last4 = card.last4
                            payment.card_exp_month = card.exp_month
                            payment.card_exp_year = card.exp_year
                    payment.receipt_url = charge_details.receipt_url
                    payment.receipt_number = charge_details.receipt_number
                    payment.billing_details = charge_details.billing_details.to_dict() if charge_details.billing_details else {}
                    payment.save()

                logger.info(f"ProcessBookingWebhook - Created Payment record {payment.id} for PI {payment_intent.id}")

                # Email notifications
                if created_bookings:
                    try:
                        send_booking_confirmation_email(user, created_bookings[0])
                        logger.info(f"ProcessBookingWebhook - Booking confirmation email prepared/queued for booking {created_bookings[0].id}, user {user.email}")
                    except Exception as email_error:
                        logger.error(f"ProcessBookingWebhook - Failed to send confirmation email for booking {created_bookings[0].id} (PI: {payment_intent.id}): {email_error}", exc_info=True)

                    try:
                        first_booking_for_notification = created_bookings[0]
                        business_info_obj = initial_instance.schedule.option.classId.businessId
                        if business_info_obj and business_info_obj.newBookingNotification:
                            owner_user = business_info_obj.owner
                            manager_users = business_info_obj.managers.all()
                            all_recipients = [owner_user] + list(manager_users)
                            unique_valid_recipients = {rec for rec in all_recipients if rec and rec.email}

                            for biz_user in unique_valid_recipients:
                                send_business_new_booking_email(biz_user, first_booking_for_notification)
                                logger.info(f"ProcessBookingWebhook - New Booking notification email prepared/queued for booking {first_booking_for_notification.id} to business user {biz_user.email}")
                        else:
                             logger.info(f"ProcessBookingWebhook - Skipped new booking notification for business {business_info_obj.businessId if business_info_obj else 'N/A'} (Setting disabled or missing owner)")
                    except AttributeError as ae_email:
                         logger.error(f"ProcessBookingWebhook - Error accessing business details for notification email for booking {created_bookings[0].id} (PI: {payment_intent.id}): {ae_email}", exc_info=True)
                    except Exception as email_error_biz:
                         logger.error(f"ProcessBookingWebhook - Failed to send new booking notification email for booking {created_bookings[0].id} (PI: {payment_intent.id}): {email_error_biz}", exc_info=True)

                return {'booking_id': created_bookings[0].id}

        except DRFValidationError:
            raise
        except Exception as e:
            logger.error(f"ProcessBookingWebhook - CRITICAL Error processing PI {payment_intent.id} within atomic block: {e}", exc_info=True)
            raise DRFValidationError("An internal error occurred while finalizing your booking.")