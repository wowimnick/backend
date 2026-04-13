# quickstart/serializers/business/business_payout_serializers.py
from rest_framework import serializers
from decimal import Decimal
from quickstart.models import Payout, Booking  # Import Booking


class PayoutBookingSerializer(serializers.ModelSerializer):
    """
    Serializer for displaying booking details within an expanded payout row.
    Handles null schedule_instance, user, and contact for robustness.
    """

    user_name = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    session_date = serializers.SerializerMethodField()
    net_amount_for_payout = serializers.SerializerMethodField()
    stripe_processing_fee = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "user_facing_reference",
            "user_name",
            "class_name",
            "session_date",
            "net_amount_for_payout",
            "stripe_processing_fee",
        ]

    def get_user_name(self, obj):
        if obj.user:
            return obj.user.get_full_name() or getattr(obj.user, "email", "") or ""
        if obj.contact:
            return (
                f"{getattr(obj.contact, 'first_name', '')} {getattr(obj.contact, 'last_name', '')}".strip()
                or getattr(obj.contact, "email", "")
                or "Guest"
            )
        return "Guest"

    def get_class_name(self, obj):
        try:
            if (
                obj.schedule_instance
                and obj.schedule_instance.schedule
                and obj.schedule_instance.schedule.option
            ):
                return obj.schedule_instance.schedule.option.classId.title
        except Exception:
            pass
        return "—"

    def get_session_date(self, obj):
        if obj.schedule_instance and obj.schedule_instance.date:
            return obj.schedule_instance.date
        return None

    def _payment_share(self, booking, payment):
        if not payment or not payment.amount or payment.amount <= 0:
            return Decimal("1.0")
        return (booking.amount_paid / payment.amount).quantize(Decimal("0.0001"))

    def get_net_amount_for_payout(self, obj):
        """
        Net payout for this booking (proportional when one Payment covers multiple bookings).
        """
        payment = obj.payments.filter(status="succeeded").first()
        if not payment:
            return Decimal("0.00")
        share = self._payment_share(obj, payment)
        return (payment.net_payout_amount * share).quantize(Decimal("0.01"))

    def get_stripe_processing_fee(self, obj):
        """This booking's share of Stripe processing fees (deducted from payout)."""
        payment = obj.payments.filter(status="succeeded").first()
        if not payment:
            return Decimal("0.00")
        share = self._payment_share(obj, payment)
        fee = payment.stripe_processing_fee or Decimal("0.00")
        return (fee * share).quantize(Decimal("0.01"))


class BusinessPayoutSerializer(serializers.ModelSerializer):
    """
    Serializer for the Payout model for business user consumption.
    """

    booking_count = serializers.IntegerField(source="booking_count_agg", read_only=True)
    amount_display = serializers.SerializerMethodField()

    class Meta:
        model = Payout
        fields = [
            "id",
            "stripe_transfer_id",
            "amount",
            "currency",
            "status",
            "arrival_date",
            "created_at",
            "booking_count",
            "amount_display",
        ]

    def get_amount_display(self, obj):
        return f"${obj.amount:,.2f} {obj.currency.upper()}"


class ScheduledPayoutSerializer(serializers.Serializer):
    """
    Read-only serializer for scheduled (projected) payouts shown in the business dashboard.
    Scheduled payouts are computed from confirmed future bookings; payout is due the day after the experience date.
    """

    id = serializers.CharField()
    stripe_transfer_id = serializers.CharField(allow_null=True)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    currency = serializers.CharField(max_length=3)
    status = serializers.CharField(default="scheduled")
    arrival_date = serializers.DateField(allow_null=True)
    created_at = serializers.DateTimeField(allow_null=True)
    booking_count = serializers.IntegerField()
    amount_display = serializers.CharField()


class PayoutSummarySerializer(serializers.Serializer):
    """
    Serializer for the aggregated payout summary data.
    """

    pending_payout_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    last_payout_amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    last_payout_date = serializers.DateField(allow_null=True)
    payouts_enabled = serializers.BooleanField()
    stripe_account_status = serializers.CharField()
    stripe_account_id = serializers.CharField(allow_blank=True, allow_null=True)
    currency = serializers.CharField(max_length=3)
