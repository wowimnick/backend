from rest_framework import serializers
from ....models import Payment, Booking, CustomUser

class AdminPaymentSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    business_name = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    booking_id = serializers.SerializerMethodField()
    card_details = serializers.SerializerMethodField()
    formatted_status = serializers.ReadOnlyField()
    available_refund_amount = serializers.ReadOnlyField()
    
    class Meta:
        model = Payment
        fields = [
            'id', 'stripe_payment_intent_id', 'stripe_charge_id', 
            'amount', 'service_fee_amount', 'currency', 'status', 'formatted_status',
            'payment_method_type', 'card_brand', 'card_last4',
            'card_exp_month', 'card_exp_year', 'card_details',
            'refunded_amount', 'refund_reason', 'refund_date',
            'receipt_url', 'receipt_number', 'available_refund_amount',
            'created_at', 'updated_at', 'booking_id', 'user_name',
            'user_email', 'business_name', 'class_name'
        ]
    
    def get_card_details(self, obj):
        if obj.card_brand and obj.card_last4:
            return {
                'brand': obj.card_brand,
                'last4': obj.card_last4,
                'exp_month': obj.card_exp_month,
                'exp_year': obj.card_exp_year,
                'display_name': f"{obj.card_brand.title()} •••• {obj.card_last4}",
                'expiry': f"{obj.card_exp_month}/{obj.card_exp_year}" if obj.card_exp_month and obj.card_exp_year else None
            }
        return None
        
    def get_booking_id(self, obj):
        if obj.booking:
            return obj.booking.id
        return None
        
    def get_user_name(self, obj):
        if obj.booking and obj.booking.user:
            user = obj.booking.user
            return f"{user.first_name} {user.last_name}".strip()
        return None
        
    def get_user_email(self, obj):
        if obj.booking and obj.booking.user:
            return obj.booking.user.email
        return None
        
    def get_business_name(self, obj):
        # Traversing from Payment -> Booking -> ScheduleInstance -> Schedule -> ClassOption -> ClassesMain -> BusinessInfo
        if obj.booking and obj.booking.schedule_instance and obj.booking.schedule_instance.schedule.option.classId.businessId:
            return obj.booking.schedule_instance.schedule.option.classId.businessId.businessName
        return None
        
    def get_class_name(self, obj):
        # Traversing from Payment -> Booking -> ScheduleInstance -> Schedule -> ClassOption -> ClassesMain
        if obj.booking and obj.booking.schedule_instance and obj.booking.schedule_instance.schedule.option.classId:
            return obj.booking.schedule_instance.schedule.option.classId.title
        return None
class AdminBookingPaymentSerializer(serializers.ModelSerializer):
    """Lightweight serializer for showing payment info in the admin booking view"""
    available_refund_amount = serializers.ReadOnlyField()
    formatted_status = serializers.ReadOnlyField()
    service_fee_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    
    class Meta:
        model = Payment
        fields = [
            'id', 'stripe_payment_intent_id', 'amount', 'service_fee_amount', 'currency', 
            'status', 'formatted_status', 'payment_method_type', 
            'card_brand', 'card_last4', 'receipt_url', 'created_at',
            'refunded_amount', 'refund_date', 'refund_reason',
            'available_refund_amount'
        ]

class AdminBookingListSerializer(serializers.ModelSerializer):
    # This serializer correctly sources its fields.
    # The 'option_name' field was changed to use the property on the ClassOption model.
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title', read_only=True, allow_null=True)
    option_name = serializers.CharField(source='schedule_instance.schedule.option.parent_class_title', read_only=True, allow_null=True) 
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email', read_only=True)
    business_name = serializers.CharField(source='schedule_instance.schedule.option.classId.businessId.businessName', read_only=True, allow_null=True)
    date = serializers.DateField(source='schedule_instance.date', read_only=True)
    time = serializers.TimeField(source='schedule_instance.time', read_only=True)
    duration = serializers.IntegerField(source='schedule_instance.duration', read_only=True) # Sourced from instance for accuracy
    session_info = serializers.SerializerMethodField()
    payment = AdminBookingPaymentSerializer(source='payments.first', read_only=True) # Using the detailed serializer for consistency
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'user_facing_reference', 'user', 'user_name', 'user_email', 'class_name', 'business_name',
            'option_name', 'date', 'time', 'duration',
            'participants', 'status', 'enrollment_type',
            'amount_paid', 'payment_status',
            'notes', 'booking_date', 'session_info', 'payment'
        ]

    def get_user_name(self, obj):
        if obj.user:
            return f"{obj.user.first_name} {obj.user.last_name}".strip()
        return "N/A"
    
    def get_session_info(self, obj):
        if obj.enrollment_type == 'Full Course' and obj.booking_group_id:
            # This logic assumes that for a course booking, all related bookings share the same group ID.
            # Getting the count can be optimized if needed, but this is functionally correct.
            related_bookings = Booking.objects.filter(booking_group_id=obj.booking_group_id).order_by('schedule_instance__date', 'schedule_instance__time')
            
            total_sessions = related_bookings.count()
            # This is a bit inefficient but works for smaller courses. For very large courses, a more optimized approach might be needed.
            try:
                current_session_index = list(related_bookings.values_list('id', flat=True)).index(obj.id) + 1
            except ValueError:
                current_session_index = '?'

            return {
                'current_session': current_session_index,
                'total_sessions': total_sessions
            }
        return None

    def get_payment(self, obj):
        payment = obj.payments.first()
        if payment:
            return {
                'id': payment.id,
                'stripe_payment_intent_id': payment.stripe_payment_intent_id,
                'amount': float(payment.amount),
                'status': payment.status,
                'payment_method_type': payment.payment_method_type,
                'card_brand': payment.card_brand,
                'card_last4': payment.card_last4
            }
        return None