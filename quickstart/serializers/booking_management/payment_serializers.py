from rest_framework import serializers
from ...models import Payment, Booking, CustomUser

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
            'amount', 'currency', 'status', 'formatted_status',
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
        if obj.booking and obj.booking.schedule_instance and obj.booking.schedule_instance.schedule.option.classId.businessId:
            return obj.booking.schedule_instance.schedule.option.classId.businessId.businessName
        return None
        
    def get_class_name(self, obj):
        if obj.booking and obj.booking.schedule_instance and obj.booking.schedule_instance.schedule.option.classId:
            return obj.booking.schedule_instance.schedule.option.classId.title
        return None

class AdminBookingPaymentSerializer(serializers.ModelSerializer):
    """Lightweight serializer for showing payment info in the admin booking view"""
    available_refund_amount = serializers.ReadOnlyField()
    formatted_status = serializers.ReadOnlyField()
    
    class Meta:
        model = Payment
        fields = [
            'id', 'stripe_payment_intent_id', 'amount', 'currency', 
            'status', 'formatted_status', 'payment_method_type', 
            'card_brand', 'card_last4', 'receipt_url', 'created_at',
            'refunded_amount', 'refund_date', 'refund_reason',
            'available_refund_amount'
        ]

class AdminBookingListSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title')
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title')
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email')
    date = serializers.DateField(source='schedule_instance.date')
    time = serializers.TimeField(source='schedule_instance.time')
    duration = serializers.IntegerField(source='schedule_instance.schedule.duration')
    booking_type = serializers.CharField(source='enrollment_type')
    session_info = serializers.SerializerMethodField()
    payment = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'id', 'user_name', 'user_email', 'class_name',
            'option_name', 'date', 'time', 'duration',
            'participants', 'status', 'enrollment_type',
            'booking_type', 'amount_paid', 'payment_status',
            'notes', 'booking_date', 'attendance_marked',
            'attended', 'session_info', 'payment'
        ]

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()
    
    def get_session_info(self, obj):
        if obj.enrollment_type == 'Full Course':
            # Get all bookings in the same group
            related_bookings = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).order_by('schedule_instance__date')
            
            total_sessions = related_bookings.count()
            current_session = list(related_bookings).index(obj) + 1
            
            return {
                'current_session': current_session,
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