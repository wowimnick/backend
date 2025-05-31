# quickstart/serializers/business/business_booking_serializers.py
from rest_framework import serializers
from ...models import Booking, CustomUser, ScheduleInstance, Schedule, ClassOption, ClassesMain, BusinessInfo, Payment # Ensure Payment is imported
import logging

logger = logging.getLogger(__name__)

# --- Nested Serializer for User Details (for Business Context) ---
class _BusinessDetailUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ['userId', 'email', 'full_name', 'phone_number', 'avatar_url', 'user_timezone'] # Added user_timezone

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()

    def get_avatar_url(self, obj):
        return obj.get_avatar_url()

# --- Nested Serializer for Class Details (Minimal) ---
class _BusinessDetailClassSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassesMain
        fields = ['classId', 'title']

# --- Nested Serializer for Option Details (Minimal, with Class Info) ---
class _BusinessDetailOptionSerializer(serializers.ModelSerializer):
    class_info = _BusinessDetailClassSerializer(source='classId', read_only=True)
    class Meta:
        model = ClassOption
        fields = ['optionId', 'title', 'booking_type', 'class_info', 'cancellationPolicy']

# --- Nested Serializer for Schedule Details (Minimal, with Option Context) ---
class _BusinessDetailScheduleSerializer(serializers.ModelSerializer):
    option_booking_type = serializers.CharField(source='option.booking_type', read_only=True)
    option_cancellation_policy = serializers.CharField(source='option.cancellationPolicy', read_only=True)
    class Meta:
        model = Schedule
        fields = ['id', 'day', 'time', 'duration', 'option_booking_type', 'option_cancellation_policy']

# --- Nested Serializer for Schedule Instance Details ---
class _BusinessDetailScheduleInstanceSerializer(serializers.ModelSerializer):
    schedule_details = _BusinessDetailScheduleSerializer(source='schedule', read_only=True)
    class Meta:
        model = ScheduleInstance
        fields = ['id', 'date', 'time', 'duration', 'price', 'max_participants', 'schedule_details']

# --- Nested Serializer for Business Context ---
class _BusinessContextSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessInfo
        fields = ['businessId', 'businessName', 'business_timezone'] # Added businessId

# --- Nested Serializer for Payment Details (for Business Context) ---
class _PaymentDetailSerializerForBusiness(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            'id', # Your internal payment ID
            'stripe_payment_intent_id',
            'status', # e.g., succeeded, refunded, pending, failed
            'amount', # Gross amount of this payment transaction
            'service_fee_amount', # The fee ClassEasily took (for business's info)
            'currency',
            'payment_method_type',
            'card_brand',
            'card_last4',
            'created_at', # Date of payment
            'receipt_url', # If available from Stripe and you want to show it
            'refunded_amount',
            'refund_reason',
            'refund_date',
            'failure_message', # If payment failed
        ]
        read_only_fields = fields


# --- Serializer for LISTING bookings (Business Context) ---
class BusinessBookingListSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title', read_only=True)
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title', read_only=True)
    user_name = serializers.SerializerMethodField(read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    date = serializers.DateField(source='schedule_instance.date', read_only=True)
    time = serializers.TimeField(source='schedule_instance.time', read_only=True)
    duration = serializers.IntegerField(source='schedule_instance.duration', read_only=True) # Changed to instance.duration
    session_info = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'user_facing_reference', 'user_name', 'user_email', 'class_name',
            'option_name', 'date', 'time', 'duration',
            'participants', 'participant_details',
            'status', 'enrollment_type',
            'amount_paid', 'payment_status',
            'notes', 'booking_date', 'attendance_marked',
            'attended', 'session_info'
        ]
        read_only_fields = fields

    def get_user_name(self, obj):
        if obj.user:
            name = f"{obj.user.first_name} {obj.user.last_name}".strip()
            return name if name else "Unnamed User" # Handle case where names might be empty
        return "Unknown User"

    def get_session_info(self, obj):
        if obj.enrollment_type == 'Full Course' and obj.booking_group_id:
            related_bookings_qs = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).order_by('schedule_instance__date', 'schedule_instance__time')

            total_sessions = related_bookings_qs.count()
            current_session_num = 'N/A'
            # Ensure comparison is against the correct object if 'obj' itself is from a queryset
            # If obj is already from related_bookings_qs, its position is its index + 1
            # If obj is an arbitrary booking, we need to find its position in the group
            
            # Efficiently find the index
            booking_ids_in_group = list(related_bookings_qs.values_list('id', flat=True))
            try:
                current_session_num = booking_ids_in_group.index(obj.id) + 1
            except ValueError:
                # This booking ID is not in the ordered list of its own group, which is unexpected.
                logger.warning(f"BusinessBookingListSerializer: Booking ID {obj.id} not found within its own group {obj.booking_group_id} during session_info calculation.")
                # Fallback: try to determine by date if only one booking on that date in group
                if total_sessions > 0:
                     bookings_on_same_date = [b for b in related_bookings_qs if b.schedule_instance.date == obj.schedule_instance.date]
                     if len(bookings_on_same_date) == 1 and bookings_on_same_date[0].id == obj.id:
                         # This is a simplified way, might not be perfect if multiple sessions on same day
                         pass # current_session_num remains 'N/A' or determined by list above
            
            return {
                'current_session': current_session_num,
                'total_sessions': total_sessions
            }
        return None

# --- Serializer for DETAIL view of a booking (Business Context) ---
class BusinessBookingDetailSerializer(serializers.ModelSerializer):
    user_details = _BusinessDetailUserSerializer(source='user', read_only=True)
    schedule_instance_details = _BusinessDetailScheduleInstanceSerializer(source='schedule_instance', read_only=True)
    business_context = _BusinessContextSerializer(source='schedule_instance.schedule.option.classId.businessId', read_only=True)
    session_info = serializers.SerializerMethodField(read_only=True)
    payment_info = serializers.SerializerMethodField(read_only=True) # New field

    # Keeping flat fields for easier consumption on frontend, alongside nested structures
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title', read_only=True)
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title', read_only=True)
    date = serializers.DateField(source='schedule_instance.date', read_only=True)
    time = serializers.TimeField(source='schedule_instance.time', read_only=True)
    duration = serializers.IntegerField(source='schedule_instance.duration', read_only=True) # Changed to instance.duration

    class Meta:
        model = Booking
        fields = [
            'id',
            'user_facing_reference', # Added user-facing reference
            'booking_group_id',
            'user_details',
            
            'class_name', # Flat field
            'option_name',# Flat field
            'date',       # Flat field
            'time',       # Flat field
            'duration',   # Flat field

            'schedule_instance_details',
            'business_context',
            
            'enrollment_type',
            'status',
            'booking_date',
            'participants',
            'participant_details',
            'notes',
            'cancelled_at',
            'cancellation_reason',
            'amount_paid',        # From Booking model
            'payment_status',     # From Booking model
            'attendance_marked',
            'attended',
            'session_info',
            'payment_info',       # Added payment details from Payment model
        ]
        read_only_fields = fields

    def get_session_info(self, obj):
        # Same logic as in BusinessBookingListSerializer
        if obj.enrollment_type == 'Full Course' and obj.booking_group_id:
            related_bookings_qs = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).select_related('schedule_instance').order_by('schedule_instance__date', 'schedule_instance__time') # Added select_related
            
            total_sessions = related_bookings_qs.count()
            current_session_num = 'N/A'
            
            booking_ids_in_group = list(related_bookings_qs.values_list('id', flat=True))
            try:
                current_session_num = booking_ids_in_group.index(obj.id) + 1
            except ValueError:
                logger.warning(f"BusinessBookingDetailSerializer: Booking ID {obj.id} not found within its own group {obj.booking_group_id} for session_info.")
            
            return {'current_session': current_session_num, 'total_sessions': total_sessions}
        return None

    def get_payment_info(self, obj: Booking):
        """
        Retrieves payment information related to this booking.
        For a course booking, the payment is typically associated with the first booking
        instance of that course group.
        """
        payment = None
        # Attempt to get payment directly linked to this specific booking first.
        # This covers single session bookings or if each course session had its own payment (unlikely with current setup).
        if obj.payments.exists(): # Using the related_name 'payments' from Payment model's ForeignKey to Booking
            payment = obj.payments.order_by('-created_at').first()
        
        # If no direct payment and it's a group booking (e.g., a course),
        # find the payment associated with the first booking in that group.
        elif obj.booking_group_id:
            first_booking_in_group = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).select_related('user' # Eager load to avoid N+1 if _PaymentDetailSerializer needs it
            ).prefetch_related('payments' # Eager load payments for the first booking
            ).order_by('booking_date', 'id').first() # Ensure consistent ordering
            
            if first_booking_in_group and first_booking_in_group.payments.exists():
                payment = first_booking_in_group.payments.order_by('-created_at').first()
                if not payment:
                    logger.warning(f"Payment info: First booking {first_booking_in_group.id} in group {obj.booking_group_id} has no payments, for target booking {obj.id}.")
            elif first_booking_in_group:
                 logger.warning(f"Payment info: First booking {first_booking_in_group.id} in group {obj.booking_group_id} exists but has no payments, for target booking {obj.id}.")
            else:
                 logger.warning(f"Payment info: No first booking found for group {obj.booking_group_id}, for target booking {obj.id}.")


        if payment:
            return _PaymentDetailSerializerForBusiness(payment).data
        
        logger.info(f"No payment information found for Booking ID {obj.id} (Group ID: {obj.booking_group_id}) after checks.")
        return None