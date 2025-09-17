# quickstart/serializers/widget/widget_serializers.py

from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.utils import timezone
import logging

from quickstart.models import (
    BusinessInfo,
    ClassesMain,
    ClassOption,
    ScheduleInstance,
    Contact,
    Booking,
)

logger = logging.getLogger(__name__)

# =============================================================================
# READ-ONLY SERIALIZERS (FOR FETCHING DATA)
# =============================================================================


class WidgetBusinessConfigSerializer(serializers.ModelSerializer):
    """
    Provides the essential, public-safe configuration data the widget needs to initialize.
    This is a READ-ONLY serializer.
    """

    class Meta:
        model = BusinessInfo
        fields = [
            "businessName",
            "business_timezone",
            "currency",
            # You can add styling fields here in the future, e.g.:
            # 'widget_primary_color',
        ]
        read_only_fields = fields


class WidgetClassOptionSerializer(serializers.ModelSerializer):
    """
    A lean, nested serializer to provide essential option details for a class.
    READ-ONLY.
    """

    class Meta:
        model = ClassOption
        fields = [
            "optionId",
            "booking_type",
            "level",
        ]
        read_only_fields = fields


class WidgetClassSerializer(serializers.ModelSerializer):
    """
    Lists the publicly bookable classes for the widget, including their options.
    This is a READ-ONLY serializer designed to be lean for list views.
    """

    options = WidgetClassOptionSerializer(many=True, read_only=True)

    class Meta:
        model = ClassesMain
        fields = [
            "classId",
            "title",
            "description",
            "options",
        ]
        read_only_fields = fields


class WidgetScheduleInstanceSerializer(serializers.ModelSerializer):
    """
    Displays a specific, available time slot for booking.
    The 'available_spots' is calculated in the view and passed here.
    This is a READ-ONLY serializer.
    """

    available_spots = serializers.SerializerMethodField()

    class Meta:
        model = ScheduleInstance
        fields = [
            "id",
            "date",
            "time",
            "duration",
            "price",
            "max_participants",
            "available_spots",
        ]
        read_only_fields = fields

    def get_available_spots(self, obj):
        # This relies on the view having already calculated the current occupancy.
        # This is a safe way to display the result of the view's logic.
        return obj.available_spots


# =============================================================================
# WRITE-ONLY SERIALIZER (FOR CREATING THE BOOKING)
# =============================================================================


class GuestBookingCreateSerializer(serializers.Serializer):
    """
    Validates the complete payload for a new guest booking from the widget.
    This is a WRITE-ONLY serializer; it does not represent an existing model but
    validates input for the creation of Contact and Booking objects.
    """

    # Fields received from the widget's frontend
    payment_intent_id = serializers.CharField(required=True, write_only=True)
    schedule_instance_id = serializers.IntegerField(required=True, write_only=True)
    participants = serializers.IntegerField(
        required=True, min_value=1, max_value=10, write_only=True
    )

    # Guest Contact Information
    first_name = serializers.CharField(max_length=150, required=True, write_only=True)
    last_name = serializers.CharField(
        max_length=150, required=False, allow_blank=True, write_only=True
    )
    email = serializers.EmailField(required=True, write_only=True)
    phone_number = serializers.CharField(
        max_length=100, required=False, allow_blank=True, write_only=True
    )

    # Optional Fields
    participant_details = serializers.ListField(
        child=serializers.DictField(), required=False, allow_empty=True, write_only=True
    )
    notes = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, write_only=True
    )

    def validate(self, data):
        """
        Performs comprehensive, production-level validation before the booking is attempted.
        """
        # 1. Validate the Schedule Instance
        try:
            # The view should pass the business context to the serializer
            business = self.context["request"].business_context
            instance = ScheduleInstance.objects.get(
                id=data["schedule_instance_id"],
                schedule__option__classId__businessId=business,
            )
        except ScheduleInstance.DoesNotExist:
            raise DRFValidationError(
                {
                    "schedule_instance_id": "The selected session is no longer available or invalid."
                }
            )

        # 2. Check if the instance is in the past or not scheduled
        if instance.status != "scheduled":
            raise DRFValidationError(
                {
                    "schedule_instance_id": "This session has been cancelled or is already complete."
                }
            )

        if instance.date < timezone.now().date():
            raise DRFValidationError(
                {
                    "schedule_instance_id": "You cannot book a session that is in the past."
                }
            )

        # 3. Validate participant count against what's available
        # Note: The ultimate race-condition check is the select_for_update() in the view,
        # but this validation provides a faster failure for the user.
        if not instance.can_accommodate(data["participants"]):
            raise DRFValidationError(
                {
                    "participants": f"Not enough spots available. Only {instance.available_spots} spots remain."
                }
            )

        # 4. Validate participant_details structure against participants count
        participants_count = data["participants"]
        participant_details = data.get("participant_details", [])

        if participants_count > 1 and not participant_details:
            raise DRFValidationError(
                {"participant_details": "Please provide the names of all participants."}
            )

        if participant_details:
            if len(participant_details) != participants_count:
                raise DRFValidationError(
                    {
                        "participant_details": f"You specified {participants_count} participants but provided details for {len(participant_details)}."
                    }
                )

            for i, detail in enumerate(participant_details):
                if (
                    not isinstance(detail, dict)
                    or "name" not in detail
                    or not str(detail["name"]).strip()
                ):
                    raise DRFValidationError(
                        {
                            "participant_details": f"A name is required for participant #{i + 1}."
                        }
                    )

        return data
