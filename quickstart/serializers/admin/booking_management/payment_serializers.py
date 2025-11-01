# quickstart/serializers/admin/booking_management/payment_serializers.py

import os
from django.conf import settings
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
            "id",
            "stripe_payment_intent_id",
            "stripe_charge_id",
            "amount",
            "platform_fee_amount",  # FIX: Renamed from service_fee_amount
            "currency",
            "status",
            "formatted_status",
            "payment_method_type",
            "card_brand",
            "card_last4",
            "card_exp_month",
            "card_exp_year",
            "card_details",
            "refunded_amount",
            "refund_reason",
            "refund_date",
            "receipt_url",
            "receipt_number",
            "available_refund_amount",
            "created_at",
            "updated_at",
            "booking_id",
            "user_name",
            "user_email",
            "business_name",
            "class_name",
        ]

    def get_card_details(self, obj):
        if obj.card_brand and obj.card_last4:
            return {
                "brand": obj.card_brand,
                "last4": obj.card_last4,
                "exp_month": obj.card_exp_month,
                "exp_year": obj.card_exp_year,
                "display_name": f"{obj.card_brand.title()} •••• {obj.card_last4}",
                "expiry": (
                    f"{obj.card_exp_month}/{obj.card_exp_year}"
                    if obj.card_exp_month and obj.card_exp_year
                    else None
                ),
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
        if (
            obj.booking
            and obj.booking.schedule_instance
            and obj.booking.schedule_instance.schedule.option.classId.businessId
        ):
            return (
                obj.booking.schedule_instance.schedule.option.classId.businessId.businessName
            )
        return None

    def get_class_name(self, obj):
        # Traversing from Payment -> Booking -> ScheduleInstance -> Schedule -> ClassOption -> ClassesMain
        if (
            obj.booking
            and obj.booking.schedule_instance
            and obj.booking.schedule_instance.schedule.option.classId
        ):
            return obj.booking.schedule_instance.schedule.option.classId.title
        return None


class AdminBookingPaymentSerializer(serializers.ModelSerializer):
    """Lightweight serializer for showing payment info in the admin booking view"""

    available_refund_amount = serializers.ReadOnlyField()
    formatted_status = serializers.ReadOnlyField()

    # --- ADD THIS ---
    card_details = serializers.SerializerMethodField()
    # --- END ADD ---

    class Meta:
        model = Payment
        fields = [
            "id",
            "stripe_payment_intent_id",
            "amount",
            "platform_fee_amount",
            "net_payout_amount",
            "currency",
            "status",
            "formatted_status",
            "payment_method_type",
            "card_details",
            "receipt_url",
            "created_at",
            "refunded_amount",
            "refund_date",
            "refund_reason",
            "available_refund_amount",
        ]

    def get_card_details(self, obj):
        if obj.card_brand and obj.card_last4:
            return {
                "brand": obj.card_brand,
                "last4": obj.card_last4,
                "exp_month": obj.card_exp_month,
                "exp_year": obj.card_exp_year,
                "display_name": f"{obj.card_brand.title()} •••• {obj.card_last4}",
                "expiry": (
                    f"{obj.card_exp_month}/{obj.card_exp_year}"
                    if obj.card_exp_month and obj.card_exp_year
                    else None
                ),
            }
        return None


class AdminBookingListSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(
        source="schedule_instance.schedule.option.classId.title",
        read_only=True,
        allow_null=True,
    )
    option_name = serializers.CharField(
        source="schedule_instance.schedule.option.parent_class_title",
        read_only=True,
        allow_null=True,
    )
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    user_avatar_thumb_url = serializers.SerializerMethodField()
    business_name = serializers.CharField(
        source="schedule_instance.schedule.option.classId.businessId.businessName",
        read_only=True,
        allow_null=True,
    )
    date = serializers.DateField(source="schedule_instance.date", read_only=True)
    time = serializers.TimeField(source="schedule_instance.time", read_only=True)
    duration = serializers.IntegerField(
        source="schedule_instance.duration", read_only=True
    )  # Sourced from instance for accuracy
    session_info = serializers.SerializerMethodField()
    payment = serializers.SerializerMethodField()
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "user_facing_reference",
            "user",
            "user_name",
            "user_email",
            "user_avatar_thumb_url",
            "class_name",
            "business_name",
            "option_name",
            "date",
            "time",
            "duration",
            "participants",
            "status",
            "enrollment_type",
            "amount_paid",
            "payment_status",
            "notes",
            "booking_date",
            "session_info",
            "payment",
            "cancellation_policy",
            "cancellation_custom_hours",
            "cancellation_refund_percentage",
        ]

    def get_user_name(self, obj):
        # Handle registered users
        if obj.user:
            return f"{obj.user.first_name} {obj.user.last_name}".strip()
        # Handle guest bookings via contact
        elif obj.contact:
            return f"{obj.contact.first_name} {obj.contact.last_name}".strip()
        return "N/A"

    def get_user_email(self, obj):
        # Handle registered users
        if obj.user:
            return obj.user.email
        # Handle guest bookings via contact
        elif obj.contact:
            return obj.contact.email
        return None

    def get_user_avatar_thumb_url(self, obj):
        # Guest contacts don't have avatars
        if not obj.user:
            return None

        if (
            not obj.user.avatar
            or not hasattr(obj.user.avatar, "name")
            or not obj.user.avatar.name
        ):
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None

        original_path = obj.user.avatar.name
        if not original_path.startswith("originals/"):
            return None

        base_path, _ = os.path.splitext(original_path)
        resized_base_path = base_path.replace("originals/", "public/thumb/", 1)
        webp_path = resized_base_path + ".webp"

        return f"{settings.CLOUDFRONT_DOMAIN}/{webp_path}"

    def get_payment(self, obj):
        payment_instance = obj.payments.first()
        if payment_instance:
            return AdminBookingPaymentSerializer(payment_instance).data
        return None

    def get_session_info(self, obj):
        if obj.enrollment_type == "Full Course":
            return {"is_course": True}
        return None
