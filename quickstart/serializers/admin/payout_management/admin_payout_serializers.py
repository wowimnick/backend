# quickstart/serializers/admin/payout_management/payout_serializers.py

from rest_framework import serializers
from quickstart.models import Payout, Booking


class AdminPayoutBookingSerializer(serializers.ModelSerializer):
    """
    Serializer for bookings listed within a payout detail view.
    Provides a concise summary of each booking included in the payout.
    """

    class_name = serializers.CharField(
        source="schedule_instance.schedule.option.classId.title", read_only=True
    )
    net_amount = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id",
            "user_facing_reference",
            "class_name",
            "amount_paid",
            "net_amount",
            "status",
            "booking_date",
        ]

    def get_net_amount(self, obj):
        """
        Calculate the net amount for the business after the platform fee.
        This assumes the fee is stored on the related Payment record.
        """
        payment = obj.payments.first()
        if payment:
            return payment.amount - payment.service_fee_amount
        return obj.amount_paid  # Fallback if no payment record found


class AdminPayoutListSerializer(serializers.ModelSerializer):
    """
    Serializer for the main list view of all payouts.
    """

    business_name = serializers.CharField(
        source="business.businessName", read_only=True
    )
    included_bookings_count = serializers.IntegerField(
        source="bookings.count", read_only=True
    )

    class Meta:
        model = Payout
        fields = [
            "id",
            "business_name",
            "stripe_transfer_id",
            "amount",
            "currency",
            "status",
            "arrival_date",
            "created_at",
            "included_bookings_count",
        ]


class AdminPayoutDetailSerializer(AdminPayoutListSerializer):
    """
    Detailed serializer for a single payout.
    Inherits from the list serializer and adds the nested list of included bookings.
    """

    bookings = AdminPayoutBookingSerializer(many=True, read_only=True)
    business_stripe_account_id = serializers.CharField(
        source="business.stripe_account_id", read_only=True
    )

    class Meta(AdminPayoutListSerializer.Meta):
        fields = AdminPayoutListSerializer.Meta.fields + [
            "bookings",
            "business_stripe_account_id",
            "metadata",
        ]
