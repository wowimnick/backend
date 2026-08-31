from decimal import Decimal, InvalidOperation
import json
import os
from django.conf import settings
from rest_framework.exceptions import PermissionDenied

from quickstart.utils.url_utils import build_cloudfront_url
from django.contrib.gis.geos import Point
from rest_framework import serializers
from rest_framework.serializers import empty
from django.db.models.functions import Coalesce
from django.db.models import Q, Sum
from django.utils import timezone
import logging


from quickstart.models import (
    Booking,
    BusinessInfo,
    BusinessLocation,
    ClassCategory,
    ClassSubcategory,
    ClassesMain,
    ClassImage,
    ClassOption,
    Schedule,
    ScheduleInstance,
)
from quickstart.utils.business_location_utils import (
    apply_business_location_to_class_instance,
)

logger = logging.getLogger(__name__)


class PublicSubcategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassSubcategory
        fields = ["name", "key"]


class PublicCategorySerializer(serializers.ModelSerializer):
    """Provides category data, including an optimized image for display."""

    image_medium_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassCategory
        fields = ["name", "key", "description", "image_medium_url", "icon_name"]

    def get_image_medium_url(self, obj):
        if not obj.image or not obj.image.name:
            return None
        original_path = obj.image.name
        if not original_path.startswith("originals/"):
            return None
        resized_path = original_path.replace("originals/", "public/medium/", 1)
        if not resized_path.endswith(".webp"):
            resized_path = os.path.splitext(resized_path)[0] + ".webp"
        return build_cloudfront_url(resized_path)


class ScheduleGroupActionSerializer(serializers.Serializer):
    """Validates the base requirement for a group action: option_id and name."""

    option_id = serializers.PrimaryKeyRelatedField(
        queryset=ClassOption.objects.all(), required=True
    )
    name = serializers.CharField(max_length=100, required=True, allow_blank=False)


class BusinessContactInfoSerializer(serializers.ModelSerializer):
    """Serializer for exposing business contact details for pre-filling forms."""

    class Meta:
        model = BusinessInfo
        fields = [
            "studentContactEmail",
            "studentContactPhone",
            "businessAddress",
            "businessUnit",
            "latitude",
            "longitude",
            "businessCity",
            "businessState",
        ]


class ClassImageSerializer(serializers.ModelSerializer):
    """Serializer for managing class images, now providing optimized URLs."""

    image_thumb_url = serializers.SerializerMethodField()
    image_medium_url = serializers.SerializerMethodField()
    image_large_url = serializers.SerializerMethodField()
    image_original_url = serializers.ImageField(
        source="image", read_only=True, use_url=True
    )

    class Meta:
        model = ClassImage
        fields = [
            "imageId",
            "image_thumb_url",
            "image_medium_url",
            "image_large_url",
            "image_original_url",
            "createdAt",
        ]
        read_only_fields = [
            "imageId",
            "createdAt",
            "image_thumb_url",
            "image_medium_url",
            "image_large_url",
            "image_original_url",
        ]

    def _get_resized_image_url(self, obj, size_name):
        if not obj.image or not obj.image.name:
            return None
        original_path = obj.image.name
        if not original_path.startswith("originals/"):
            return None

        base_path = os.path.splitext(original_path)[0]

        resized_path = (
            base_path.replace("originals/", f"public/{size_name}/", 1) + ".webp"
        )
        return build_cloudfront_url(resized_path)

    def get_image_thumb_url(self, obj):
        return self._get_resized_image_url(obj, "thumb")

    def get_image_medium_url(self, obj):
        return self._get_resized_image_url(obj, "medium")

    def get_image_large_url(self, obj):
        return self._get_resized_image_url(obj, "large")


class ScheduleInstanceSerializer(serializers.ModelSerializer):
    """Serializer for managing schedule instances (e.g., cancel, mark attendance)."""

    current_bookings_count = serializers.IntegerField(read_only=True)
    available_spots = serializers.SerializerMethodField(read_only=True)

    class_name = serializers.CharField(
        source="schedule.option.classId.title", read_only=True
    )
    class_id = serializers.IntegerField(
        source="schedule.option.classId.classId", read_only=True
    )
    booking_type = serializers.CharField(
        source="schedule.option.booking_type", read_only=True
    )
    schedule_name = serializers.CharField(
        source="schedule.name", read_only=True, allow_null=True
    )

    assigned_staff_id = serializers.UUIDField(
        source="assigned_staff_id", read_only=True, allow_null=True
    )
    recurrence_rule_id = serializers.UUIDField(
        source="recurrence_rule_id", read_only=True, allow_null=True
    )

    class Meta:
        model = ScheduleInstance
        fields = [
            "id",
            "schedule",
            "date",
            "time",
            "duration",
            "price",
            "max_participants",
            "min_participants",
            "status",
            "cancellation_reason",
            "current_bookings_count",
            "available_spots",
            "created_at",
            "updated_at",
            "class_name",
            "class_id",
            "booking_type",
            "schedule_name",
            "assigned_staff_id",
            "recurrence_rule_id",
        ]
        read_only_fields = [
            "id",
            "schedule",
            "created_at",
            "updated_at",
            "current_bookings_count",
            "available_spots",
            "class_name",
            "class_id",
            "booking_type",
            "schedule_name",
        ]

    def get_available_spots(self, obj):
        current_bookings = getattr(obj, "current_bookings_count", 0)
        return obj.max_participants - current_bookings


class ScheduleSerializer(serializers.ModelSerializer):
    """Serializer for creating/managing schedules within a class option."""

    booked_participants = serializers.IntegerField(read_only=True)
    total_revenue = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    has_confirmed_bookings = serializers.BooleanField(read_only=True)

    class Meta:
        model = Schedule
        fields = [
            "id",
            "name",
            "option",
            "day",
            "time",
            "duration",
            "price",
            "maxParticipants",
            "minParticipants",
            "start_date",
            "end_date",
            "date",
            "allow_late_enrollment",
            "booked_participants",
            "total_revenue",
            "has_confirmed_bookings",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "booked_participants",
            "total_revenue",
            "has_confirmed_bookings",
        ]
        extra_kwargs = {"option": {"write_only": True}}

    def validate(self, data):
        # 1. Resolve Option and Instance Context
        instance = self.instance
        option = data.get("option") or getattr(instance, "option", None)

        if not option:
            raise serializers.ValidationError(
                "Option context is required for schedule validation."
            )

        booking_type = option.booking_type
        if booking_type == "Full Course" and not instance:
            raise serializers.ValidationError(
                "Multi-session courses can no longer be created. Use a recurring series on a group service."
            )

        # 2. Resolve Fields (Incoming Data -> Fallback to Instance -> None)
        booking_type = option.booking_type

        start_date = data.get(
            "start_date", getattr(instance, "start_date", None) if instance else None
        )
        end_date = data.get(
            "end_date", getattr(instance, "end_date", None) if instance else None
        )
        date_field = data.get(
            "date", getattr(instance, "date", None) if instance else None
        )
        day = data.get("day", getattr(instance, "day", None) if instance else None)

        # 3. Critical Change Protection (If bookings exist)
        if instance and instance.pk:
            has_bookings = getattr(instance, "has_confirmed_bookings", None)
            if has_bookings is None:
                has_bookings = Booking.objects.filter(
                    schedule_instance__schedule=instance, status="confirmed"
                ).exists()

            if has_bookings:
                # ADDED 'date' and 'duration' to this list to prevent calendar corruption
                immutable_fields = [
                    "start_date",
                    "end_date",
                    "date",
                    "day",
                    "time",
                    "duration",
                ]
                for field in immutable_fields:
                    # Check if field is present in data AND differs from stored value
                    if field in data and data[field] != getattr(instance, field):
                        raise serializers.ValidationError(
                            {
                                field: f"Cannot change the {field.replace('_', ' ')} because this schedule has confirmed bookings."
                            }
                        )

        # 4. Logic Validation based on Booking Type
        if booking_type == "Full Course":
            if not all([start_date, end_date, day]):
                raise serializers.ValidationError(
                    "Start date, end date, and day required for courses."
                )
            if start_date >= end_date:
                raise serializers.ValidationError(
                    "Course end date must be after start date."
                )
            # Only check past dates on creation, not update
            if not instance and start_date and start_date < timezone.now().date():
                raise serializers.ValidationError(
                    {"start_date": "New course cannot start in the past."}
                )

        else:  # Single Session
            if not date_field:
                raise serializers.ValidationError(
                    "Date is required for single sessions."
                )
            if date_field and not data.get("day"):
                data["day"] = date_field.strftime("%a")
            if not instance and date_field and date_field < timezone.now().date():
                raise serializers.ValidationError(
                    {"date": "New session cannot be scheduled in the past."}
                )

        # 5. Common Validations
        if data.get("duration", getattr(instance, "duration", 60)) < 15:
            raise serializers.ValidationError(
                {"duration": "Duration must be at least 15 minutes."}
            )

        new_max = data.get("maxParticipants")
        if new_max is not None:
            if new_max < 1:
                raise serializers.ValidationError(
                    {"maxParticipants": "Max participants must be at least 1."}
                )
            # Ensure capacity isn't lowered below current booking count
            if instance:
                current_booked = getattr(instance, "booked_participants", None)
                if current_booked is None:
                    current_booked = Booking.objects.filter(
                        schedule_instance__schedule=instance,
                        status="confirmed",
                    ).aggregate(total=Coalesce(Sum("participants"), 0))["total"]

                if new_max < current_booked:
                    raise serializers.ValidationError(
                        {
                            "maxParticipants": f"Cannot set capacity below confirmed bookings ({current_booked})."
                        }
                    )

        price_val = data.get("price")
        if instance is not None and price_val is None:
            price_val = getattr(instance, "price", None)
        if price_val is None:
            raise serializers.ValidationError({"price": "Price is required."})
        try:
            price_dec = Decimal(str(price_val))
        except (InvalidOperation, TypeError, ValueError):
            raise serializers.ValidationError({"price": "Invalid price."})
        if price_dec <= 0:
            raise serializers.ValidationError(
                {
                    "price": "Price must be greater than zero. Free sessions are not supported."
                }
            )

        min_p = data.get("minParticipants", getattr(instance, "minParticipants", 1))
        max_p = data.get("maxParticipants", getattr(instance, "maxParticipants", 10))

        if min_p > max_p:
            raise serializers.ValidationError(
                "Minimum participants cannot exceed maximum capacity."
            )

        return data

    def update(self, instance, validated_data):
        """
        Handle updates, including regenerating instances if course dates/days change.
        """
        # Check if core scheduling fields are changing
        old_start = instance.start_date
        old_end = instance.end_date
        old_day = instance.day
        old_time = instance.time

        # Perform the standard update
        instance = super().update(instance, validated_data)

        is_course = instance.option.booking_type == "Full Course"

        # Logic to regenerate instances if dates/times changed for a Course
        if is_course:
            core_changed = (
                instance.start_date != old_start
                or instance.end_date != old_end
                or instance.day != old_day
                or instance.time != old_time
            )

            if core_changed:
                # Delete future instances (validation already ensured no bookings exist)
                today = timezone.now().date()
                instance.instances.filter(date__gte=today, status="scheduled").delete()

                # Regenerate based on new settings
                instance.generate_course_instances()

        return instance


class BulkScheduleCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    option = serializers.PrimaryKeyRelatedField(
        queryset=ClassOption.objects.all(),
        required=True,
        help_text="The ClassOption ID to which these schedules will be attached.",
    )
    start_date = serializers.DateField(required=True)
    end_date = serializers.DateField(required=True)
    days_of_week = serializers.ListField(
        child=serializers.ChoiceField(
            choices=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        ),
        allow_empty=False,
        required=True,
    )
    times = serializers.ListField(
        child=serializers.TimeField(format="%H:%M"),
        allow_empty=False,
        required=True,
        help_text="A list of start times (HH:MM) to create for each selected day.",
    )
    duration = serializers.IntegerField(required=True, min_value=15)
    price = serializers.DecimalField(
        required=True, max_digits=10, decimal_places=2, min_value=Decimal("0.01")
    )
    maxParticipants = serializers.IntegerField(required=True, min_value=1)
    # --- FIX: Removed 'default=1' ---
    minParticipants = serializers.IntegerField(required=True, min_value=1)

    def validate(self, data):
        request = self.context.get("request")
        if not request:
            raise serializers.ValidationError(
                "Request context is required for validation."
            )
        user = request.user
        option = data["option"]

        if user.has_perm("quickstart.access_class_admin"):
            pass
        else:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()

            if not business or option.classId.businessId != business:
                raise PermissionDenied(
                    "You do not have permission to create schedules for this class option."
                )
        if data["start_date"] > data["end_date"]:
            raise serializers.ValidationError(
                {"end_date": "End date must be on or after start date."}
            )
        if data["start_date"] < timezone.now().date():
            raise serializers.ValidationError(
                {"start_date": "Bulk creation cannot start in the past."}
            )
        if data.get("minParticipants", 1) > data.get("maxParticipants", 1):
            raise serializers.ValidationError(
                "Minimum participants cannot exceed maximum capacity."
            )

        return data


class ManagedClassOptionSerializer(serializers.ModelSerializer):
    """
    Serializer for managing class options by business users.
    """

    class Meta:
        model = ClassOption
        fields = [
            "optionId",
            "classId",
            "title",
            "description",
            "duration_minutes",
            "price",
            "capacity",
            "booking_type",
            "level",
            "equipment",
            "tags",
            "cancellationPolicy",
            "cancellationCustomHours",
            "cancellationRefundPercentage",
            "price_type",
            "createdAt",
            "updatedAt",
        ]
        read_only_fields = [
            "optionId",
            "classId",
            "createdAt",
            "updatedAt",
        ]
        extra_kwargs = {
            "title": {"required": False},
            "equipment": {"required": False},
            "tags": {"required": False},
            "duration_minutes": {"required": False, "allow_null": True},
            "price": {"required": False, "allow_null": True},
            "capacity": {"required": False, "allow_null": True},
            "level": {"default": ClassOption._meta.get_field("level").get_default()},
            "cancellationPolicy": {
                "default": ClassOption._meta.get_field(
                    "cancellationPolicy"
                ).get_default()
            },
            "cancellationRefundPercentage": {
                "default": ClassOption._meta.get_field(
                    "cancellationRefundPercentage"
                ).get_default()
            },
            "booking_type": {
                "default": ClassOption._meta.get_field("booking_type").get_default()
            },
            "price_type": {
                "default": ClassOption._meta.get_field("price_type").get_default()
            },
        }

    def validate(self, data):
        if data.get("booking_type") == "Full Course":
            raise serializers.ValidationError(
                {
                    "booking_type": "Multi-session courses can no longer be created."
                }
            )
        policy = data.get(
            "cancellationPolicy", getattr(self.instance, "cancellationPolicy", None)
        )
        custom_hours = data.get("cancellationCustomHours")

        if policy == "custom":
            if custom_hours is None:
                raise serializers.ValidationError(
                    {
                        "cancellationCustomHours": "A custom hour value is required when the policy is 'Custom'."
                    }
                )
            if custom_hours < 1:
                raise serializers.ValidationError(
                    {"cancellationCustomHours": "Custom hours must be at least 1."}
                )

        # --- ADDED: Validation for mid-course drop policies ---
        allow_drops = data.get(
            "allowMidCourseDrops",
            getattr(self.instance, "allowMidCourseDrops", False),
        )
        mid_course_policy = data.get("midCourseCancellationPolicy")
        mid_course_custom_hours = data.get("midCourseCancellationCustomHours")

        # If drops are allowed, a policy is required.
        if allow_drops and not mid_course_policy:
            raise serializers.ValidationError(
                {
                    "midCourseCancellationPolicy": "A cancellation policy is required if mid-course drops are allowed."
                }
            )

        # If the mid-course policy is 'custom', custom hours must be provided.
        if allow_drops and mid_course_policy == "custom":
            if mid_course_custom_hours is None:
                raise serializers.ValidationError(
                    {
                        "midCourseCancellationCustomHours": "Custom hours are required for a 'custom' mid-course drop policy."
                    }
                )
            if mid_course_custom_hours < 1:
                raise serializers.ValidationError(
                    {
                        "midCourseCancellationCustomHours": "Mid-course custom hours must be at least 1."
                    }
                )

        return data

    def validate_equipment(self, value):
        """Normalize equipment to string (packing list is free-form text for the booker)."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "\n".join(str(item).strip() for item in value if item).strip()
        return ""

    def to_representation(self, instance):
        """Ensure equipment is always returned as string for API consumers."""
        ret = super().to_representation(instance)
        equipment = ret.get("equipment")
        if isinstance(equipment, list):
            ret["equipment"] = "\n".join(str(item) for item in equipment).strip()
        elif equipment is None:
            ret["equipment"] = ""
        return ret


class ManagedClassSerializer(serializers.ModelSerializer):
    """Serializer for business users managing their classes."""

    options = ManagedClassOptionSerializer(many=True, read_only=True)
    images = ClassImageSerializer(many=True, read_only=True)
    business_name = serializers.CharField(
        source="businessId.businessName", read_only=True
    )
    last_schedule_date = serializers.DateField(read_only=True, allow_null=True)
    location_ref = serializers.PrimaryKeyRelatedField(
        queryset=BusinessLocation.objects.all(),
        allow_null=True,
        required=False,
    )
    location_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = ClassesMain
        fields = [
            "classId",
            "slug",
            "businessId",
            "title",
            "description",
            "service_type",
            "duration_minutes",
            "price",
            "capacity",
            "buffer_before_minutes",
            "buffer_after_minutes",
            "slot_interval_minutes",
            "max_concurrent",
            "min_notice_hours",
            "max_advance_days",
            "cancellationPolicy",
            "cancellationCustomHours",
            "cancellationRefundPercentage",
            "status",
            "unit_number",
            "location",
            "city",
            "state",
            "coordinates",
            "saltLocation",
            "location_ref",
            "location_name",
            "studentContactEmail",
            "studentContactPhone",
            "adminContactEmail",
            "adminContactPhone",
            "createdAt",
            "updatedAt",
            "options",
            "images",
            "business_name",
            "last_schedule_date",
        ]
        read_only_fields = [
            "classId",
            "slug",
            "businessId",
            "createdAt",
            "updatedAt",
            "options",
            "images",
            "business_name",
            "last_schedule_date",
            "location_name",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = (self.context or {}).get("business")
        if business is not None:
            self.fields["location_ref"].queryset = BusinessLocation.objects.filter(
                business=business
            )
        elif getattr(self, "instance", None) is not None and getattr(
            self.instance, "businessId_id", None
        ):
            self.fields["location_ref"].queryset = BusinessLocation.objects.filter(
                business_id=self.instance.businessId_id
            )
        else:
            self.fields["location_ref"].queryset = BusinessLocation.objects.none()

    def get_location_name(self, obj):
        ref = getattr(obj, "location_ref", None)
        return ref.name if ref else None

    def update(self, instance, validated_data):
        location_ref = validated_data.pop("location_ref", empty)
        if "features" in validated_data and isinstance(validated_data["features"], str):
            try:
                validated_data["features"] = json.loads(validated_data["features"])
            except json.JSONDecodeError:
                raise serializers.ValidationError(
                    {
                        "features": "Invalid format. Features must be a valid JSON array string."
                    }
                )

        if "coordinates" in validated_data:
            coordinates_str = validated_data.get("coordinates")
            if (
                coordinates_str
                and isinstance(coordinates_str, str)
                and coordinates_str.lower() != "undefined"
            ):
                try:
                    lat_str, lng_str = map(str.strip, coordinates_str.split(","))
                    validated_data["latitude"] = Decimal(lat_str)
                    validated_data["longitude"] = Decimal(lng_str)
                except (ValueError, InvalidOperation) as e:
                    logger.warning(
                        f"During class update (ID: {instance.pk}), could not parse coordinates: '{coordinates_str}'. Error: {e}. Existing coordinates will be preserved."
                    )
        instance = super().update(instance, validated_data)
        if location_ref is not empty:
            if location_ref is None:
                instance.location_ref = None
                instance.save(update_fields=["location_ref"])
            else:
                apply_business_location_to_class_instance(instance, location_ref)
                instance.save()
        return instance


class ClassCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new classes. Use collections for grouping."""

    city = serializers.CharField(required=False, allow_blank=True, max_length=100)
    state = serializers.CharField(required=False, allow_blank=True, max_length=100)
    location_ref = serializers.PrimaryKeyRelatedField(
        queryset=BusinessLocation.objects.all(),
        allow_null=True,
        required=False,
    )

    class Meta:
        model = ClassesMain
        fields = [
            "title",
            "description",
            "service_type",
            "duration_minutes",
            "price",
            "capacity",
            "buffer_before_minutes",
            "buffer_after_minutes",
            "slot_interval_minutes",
            "max_concurrent",
            "min_notice_hours",
            "max_advance_days",
            "cancellationPolicy",
            "cancellationCustomHours",
            "cancellationRefundPercentage",
            "location",
            "unit_number",
            "coordinates",
            "city",
            "state",
            "saltLocation",
            "location_ref",
            "studentContactEmail",
            "studentContactPhone",
            "adminContactEmail",
            "adminContactPhone",
        ]
        extra_kwargs = {
            "description": {"required": False, "allow_blank": True},
            "location": {"required": False, "allow_blank": True},
            "coordinates": {"required": False, "allow_blank": True},
            "service_type": {"required": False},
            "duration_minutes": {"required": False},
            "price": {"required": True},
            "capacity": {"required": False},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = (self.context or {}).get("business")
        if business is not None:
            self.fields["location_ref"].queryset = BusinessLocation.objects.filter(
                business=business, is_active=True
            )
        else:
            self.fields["location_ref"].queryset = BusinessLocation.objects.none()

    def validate(self, data):
        # Category/subcategory deprecated; ignore if sent
        return data

    def create(self, validated_data):
        location_ref = validated_data.pop("location_ref", None)
        coordinates_str = validated_data.get("coordinates")
        point = None
        if coordinates_str:
            try:
                lng_str, lat_str = map(str.strip, coordinates_str.split(","))
                point = Point(float(lng_str), float(lat_str), srid=4326)
            except (ValueError, TypeError):
                logger.warning(
                    f"Could not parse coordinates on create: '{coordinates_str}'. Point will not be set."
                )
        validated_data["point"] = point

        instance = ClassesMain.objects.create(**validated_data)
        if location_ref:
            apply_business_location_to_class_instance(instance, location_ref)
            instance.save()
        return instance
