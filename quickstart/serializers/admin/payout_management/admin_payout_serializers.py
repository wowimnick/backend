from rest_framework import serializers
from quickstart.models import Payout, Booking, Payment


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
        Returns the pre-calculated net payout amount for the business from the
        associated Payment record for this specific booking.
        """
        # Find the successful payment associated with this booking
        payment = obj.payments.filter(status="succeeded").first()
        if payment:
            # --- FIX: Use the correct field 'net_payout_amount' ---
            # This field already contains the final calculated amount due to the business for this booking.
            return payment.net_payout_amount

        # Fallback for older data or if payment somehow isn't found
        return obj.amount_paid


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
