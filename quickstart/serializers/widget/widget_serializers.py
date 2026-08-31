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
    ClassImage,
    Schedule,
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

    # CRITICAL CHANGE: This sources data from the `widget_config` JSONField on the model
    # but presents it as `theme` in the API response, matching what the frontend expects.
    theme = serializers.JSONField(source="widget_config")
    widget_fee_percentage = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            "businessName",
            "business_timezone",
            "currency",
            "theme",
            "widget_fee_percentage",
        ]
        read_only_fields = fields

    def get_widget_fee_percentage(self, obj):
        from quickstart.utils.commission import get_plan_fee_percentage
        return float(get_plan_fee_percentage(obj))


class WidgetScheduleSerializer(serializers.ModelSerializer):
    """Serializer for publicly displaying basic schedule info for summary."""

    class Meta:
        model = Schedule
        fields = [
            "id",
            "duration",
            "price",
            "maxParticipants",
        ]
        read_only_fields = fields


class WidgetClassOptionSerializer(serializers.ModelSerializer):
    """
    A lean, nested serializer to provide essential option details for a class.
    READ-ONLY.
    """

    schedules = WidgetScheduleSerializer(many=True, read_only=True)
    cancellation_policy = serializers.CharField(source="cancellationPolicy", read_only=True)
    cancellation_refund_percentage = serializers.IntegerField(source="cancellationRefundPercentage", read_only=True)
    cancellation_custom_hours = serializers.IntegerField(source="cancellationCustomHours", read_only=True, allow_null=True)

    class Meta:
        model = ClassOption
        fields = [
            "optionId",
            "title",
            "booking_type",
            "level",
            "duration_minutes",
            "price",
            "capacity",
            "schedules",
            "cancellation_policy",
            "cancellation_refund_percentage",
            "cancellation_custom_hours",
        ]
        read_only_fields = fields


class WidgetClassImageSerializer(serializers.ModelSerializer):
    """
    Serializer for the class image, providing the necessary URL for the frontend.
    """

    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassImage
        fields = ["thumbnail_url"]

    def get_thumbnail_url(self, obj):
        if obj.image and hasattr(obj.image, "url"):
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.image.url)
            return obj.image.url  # Fallback
        return None


class WidgetClassSerializer(serializers.ModelSerializer):
    """
    Lists the publicly bookable classes for the widget, including their options.
    This is a READ-ONLY serializer designed to be lean for list views.
    """

    options = WidgetClassOptionSerializer(many=True, read_only=True)
    images = WidgetClassImageSerializer(many=True, read_only=True)
    require_participant_names = serializers.BooleanField(
        source="businessId.require_participant_names",
        read_only=True,
        default=False,
    )
    location_name = serializers.SerializerMethodField()
    location_address = serializers.SerializerMethodField()

    class Meta:
        model = ClassesMain
        fields = [
            "classId",
            "title",
            "description",
            "service_type",
            "duration_minutes",
            "price",
            "capacity",
            "location",
            "location_name",
            "location_address",
            "options",
            "images",
            "require_participant_names",
        ]
        read_only_fields = fields

    def get_location_name(self, obj):
        ref = getattr(obj, "location_ref", None)
        return ref.name if ref else None

    def get_location_address(self, obj):
        return obj.location or ""


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
        if not instance.can_accommodate(data["participants"]):
            raise DRFValidationError(
                {
                    "participants": f"Not enough spots available. Only {instance.available_spots} spots remain."
                }
            )

        # 4. Validate participant_details structure when provided (optional; booker-only is allowed)
        participants_count = data["participants"]
        participant_details = data.get("participant_details", [])

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


class GuestFreeBookingCreateSerializer(serializers.Serializer):
    """
    Validates payload for a free (no payment) guest booking from the widget.
    Same validation as GuestBookingCreateSerializer for instance/participants, minus payment_intent_id.
    """

    schedule_instance_id = serializers.IntegerField(required=True, write_only=True)
    participants = serializers.IntegerField(
        required=True, min_value=1, max_value=10, write_only=True
    )
    first_name = serializers.CharField(max_length=150, required=True, write_only=True)
    last_name = serializers.CharField(
        max_length=150, required=False, allow_blank=True, write_only=True
    )
    email = serializers.EmailField(required=True, write_only=True)
    phone_number = serializers.CharField(
        max_length=100, required=False, allow_blank=True, write_only=True
    )
    participant_details = serializers.ListField(
        child=serializers.DictField(), required=False, allow_empty=True, write_only=True
    )
    notes = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, write_only=True
    )
    applied_discount_id = serializers.IntegerField(required=False, allow_null=True, write_only=True)
    discount_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True, write_only=True
    )

    def validate(self, data):
        business = self.context["request"].business_context
        try:
            instance = ScheduleInstance.objects.get(
                id=data["schedule_instance_id"],
                schedule__option__classId__businessId=business,
            )
        except ScheduleInstance.DoesNotExist:
            raise DRFValidationError(
                {"schedule_instance_id": "The selected session is no longer available or invalid."}
            )
        if instance.status != "scheduled":
            raise DRFValidationError(
                {"schedule_instance_id": "This session has been cancelled or is already complete."}
            )
        if instance.date < timezone.now().date():
            raise DRFValidationError(
                {"schedule_instance_id": "You cannot book a session that is in the past."}
            )
        if not instance.can_accommodate(data["participants"]):
            raise DRFValidationError(
                {"participants": f"Not enough spots available. Only {instance.available_spots} spots remain."}
            )
        participants_count = data["participants"]
        participant_details = data.get("participant_details", [])
        if participant_details and len(participant_details) != participants_count:
            raise DRFValidationError(
                {"participant_details": f"You specified {participants_count} participants but provided details for {len(participant_details)}."}
            )
        for i, detail in enumerate(participant_details or []):
            if not isinstance(detail, dict) or "name" not in detail or not str(detail.get("name", "")).strip():
                raise DRFValidationError(
                    {"participant_details": f"A name is required for participant #{i + 1}."}
                )
        return data
