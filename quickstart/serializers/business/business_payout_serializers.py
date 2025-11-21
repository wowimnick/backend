# quickstart/serializers/business/business_payout_serializers.py
from rest_framework import serializers
from decimal import Decimal
from quickstart.models import Payout, Booking  # Import Booking


class PayoutBookingSerializer(serializers.ModelSerializer):
    """
    Serializer for displaying booking details within an expanded payout row.
    """

    user_name = serializers.CharField(source="user.get_full_name", read_only=True)
    class_name = serializers.CharField(
        source="schedule_instance.schedule.option.classId.title", read_only=True
    )
    session_date = serializers.DateField(
        source="schedule_instance.date", read_only=True
    )
    net_amount_for_payout = serializers.SerializerMethodField()
    enrollment_type = serializers.CharField(source="enrollment_type", read_only=True)
    course_session_number = serializers.IntegerField(source="course_session_number", read_only=True)
    total_sessions = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "user_facing_reference",
            "user_name",
            "class_name",
            "session_date",
            "net_amount_for_payout",
            "enrollment_type",
            "course_session_number",
            "total_sessions"
        ]

    def get_total_sessions(self, obj):
        if obj.enrollment_type == "Full Course" and obj.booking_group_id:
            # Optimization: This could be pre-fetched, but for now:
            return obj.sibling_bookings.count() + 1
        return 1

    def get_net_amount_for_payout(self, obj):
        """
        Returns the pre-calculated net payout amount for the business from the
        associated Payment record for this specific booking.
        """
        payment = obj.payments.filter(status="succeeded").first()
        if payment:
            return payment.net_payout_amount

        return Decimal("0.00")


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
