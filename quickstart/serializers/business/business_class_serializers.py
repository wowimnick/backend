from decimal import Decimal, InvalidOperation
import json
import os
from django.conf import settings
from rest_framework.exceptions import PermissionDenied
from rest_framework import serializers
from django.db.models.functions import Coalesce
from django.db.models import Q, Sum
from django.utils import timezone
import logging


from quickstart.models import (
    Booking,
    BusinessInfo,
    ClassCategory,
    ClassSubcategory,
    ClassesMain,
    ClassImage,
    ClassOption,
    Schedule,
    ScheduleInstance,
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
        return f"{settings.CLOUDFRONT_DOMAIN}/{resized_path}"


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
            "latitude",
            "longitude",
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
        return f"{settings.CLOUDFRONT_DOMAIN}/{resized_path}"

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

    booked_participants = serializers.SerializerMethodField()
    total_revenue = serializers.SerializerMethodField()
    has_confirmed_bookings = serializers.SerializerMethodField()

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

    def get_booked_participants(self, obj):
        return Booking.objects.filter(
            schedule_instance__schedule=obj, status="confirmed"
        ).aggregate(total_booked=Coalesce(Sum("participants"), 0))["total_booked"]

    def get_total_revenue(self, obj):
        return Booking.objects.filter(
            schedule_instance__schedule=obj, status="confirmed", payment_status="paid"
        ).aggregate(total_revenue=Coalesce(Sum("amount_paid"), Decimal("0.00")))[
            "total_revenue"
        ]

    def get_has_confirmed_bookings(self, obj):
        return Booking.objects.filter(
            schedule_instance__schedule=obj, status="confirmed"
        ).exists()

    def validate(self, data):
        option = data.get("option") or getattr(self.instance, "option", None)
        if not option:
            raise serializers.ValidationError(
                "Option context is required for schedule validation."
            )

        booking_type = option.booking_type
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        date_field = data.get("date")
        day = data.get("day")

        if self.instance and self.instance.pk:
            critical_fields_being_changed = any(
                data.get(field) is not None
                and data.get(field) != getattr(self.instance, field)
                for field in [
                    "day",
                    "time",
                    "duration",
                    "price",
                    "start_date",
                    "end_date",
                    "date",
                ]
            )
            if critical_fields_being_changed:
                if Booking.objects.filter(
                    schedule_instance__schedule=self.instance, status="confirmed"
                ).exists():
                    raise serializers.ValidationError(
                        "This schedule has confirmed bookings and critical details (like date, time, price) cannot be changed. "
                        "Please cancel the existing schedule and create a new one if significant changes are needed."
                    )

        if booking_type == "Full Course":
            if not all([start_date, end_date, day]):
                raise serializers.ValidationError(
                    "Start date, end date, and day required for courses."
                )
            if start_date >= end_date:
                raise serializers.ValidationError(
                    "Course end date must be after start date."
                )
            if (
                self.instance is None or self.instance.pk is None
            ) and start_date < timezone.now().date():
                raise serializers.ValidationError(
                    {"start_date": "New course cannot start in the past."}
                )

        else:
            if not date_field:
                raise serializers.ValidationError(
                    "Date is required for single sessions."
                )
            if date_field and not data.get("day"):
                data["day"] = date_field.strftime("%a")
            if (
                self.instance is None or self.instance.pk is None
            ) and date_field < timezone.now().date():
                raise serializers.ValidationError(
                    {"date": "New session cannot be scheduled in the past."}
                )

        if data.get("duration", 60) < 15:
            raise serializers.ValidationError(
                {"duration": "Duration must be at least 15 minutes."}
            )

        new_max_participants = data.get("maxParticipants")
        if new_max_participants is not None:
            if new_max_participants < 1:
                raise serializers.ValidationError(
                    {"maxParticipants": "Max participants must be at least 1."}
                )
            if self.instance and self.instance.pk:
                current_booked_sum = Booking.objects.filter(
                    schedule_instance__schedule=self.instance, status="confirmed"
                ).aggregate(total_booked=Coalesce(Sum("participants"), 0))[
                    "total_booked"
                ]
                if new_max_participants < current_booked_sum:
                    raise serializers.ValidationError(
                        {
                            "maxParticipants": f"Cannot set capacity below current confirmed bookings ({current_booked_sum})."
                        }
                    )

        if data.get("price", 0) < 0:
            raise serializers.ValidationError({"price": "Price cannot be negative."})

        return data


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
        required=True, max_digits=10, decimal_places=2, min_value=Decimal("0.00")
    )
    maxParticipants = serializers.IntegerField(required=True, min_value=1)

    def validate(self, data):
        request = self.context.get("request")
        if not request:
            raise serializers.ValidationError(
                "Request context is required for validation."
            )
        user = request.user
        option = data["option"]
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
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
        return data


class ManagedClassOptionSerializer(serializers.ModelSerializer):
    """
    Serializer for managing class options by business users.
    MODIFIED: This no longer includes the 'schedules' field to keep the class list API response lean.
    Schedules are now fetched on-demand from the BusinessScheduleViewSet.
    """

    class Meta:
        model = ClassOption
        fields = [
            "optionId",
            "classId",
            "booking_type",
            "level",
            "equipment",
            "tags",
            "cancellationPolicy",
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
            "equipment": {"required": False},
            "tags": {"required": False},
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


class ManagedClassSerializer(serializers.ModelSerializer):
    """Serializer for business users managing their classes."""

    options = ManagedClassOptionSerializer(many=True, read_only=True)
    images = ClassImageSerializer(many=True, read_only=True)
    business_name = serializers.CharField(
        source="businessId.businessName", read_only=True
    )
    category_key = serializers.CharField(
        source="category.key", read_only=True, allow_null=True
    )
    category_name = serializers.CharField(
        source="category.name", read_only=True, allow_null=True
    )
    subcategory_key = serializers.CharField(
        source="subcategory.key", read_only=True, allow_null=True
    )
    subcategory_name = serializers.CharField(
        source="subcategory.name", read_only=True, allow_null=True
    )
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ClassesMain

        fields = [
            "classId",
            "businessId",
            "title",
            "description",
            "features",
            "category",
            "subcategory",
            "status",
            "location",
            "coordinates",
            "saltLocation",
            "studentContactEmail",
            "studentContactPhone",
            "adminContactEmail",
            "adminContactPhone",
            "createdAt",
            "updatedAt",
            "options",
            "images",
            "business_name",
            "category_key",
            "category_name",
            "subcategory_key",
            "subcategory_name",
            "average_rating",
            "review_count",
        ]
        read_only_fields = [
            "classId",
            "businessId",
            "createdAt",
            "updatedAt",
            "options",
            "images",
            "business_name",
            "category_key",
            "category_name",
            "subcategory_key",
            "subcategory_name",
            "average_rating",
            "review_count",
        ]

    def update(self, instance, validated_data):
        """
        Custom update to handle parsing 'coordinates' into latitude and longitude,
        and to correctly handle stringified JSON for 'features'.
        This is now NON-DESTRUCTIVE for coordinates.
        """

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

            validated_data.pop("coordinates", None)

        return super().update(instance, validated_data)


class ClassCreateSerializer(serializers.ModelSerializer):
    """Serializer specifically for creating new classes."""

    category_key = serializers.CharField(write_only=True, required=True)
    subcategory_key = serializers.CharField(
        write_only=True, required=True, allow_blank=False
    )

    class Meta:
        model = ClassesMain
        fields = [
            "title",
            "description",
            "features",
            "category_key",
            "subcategory_key",
            "location",
            "coordinates",
            "saltLocation",
            "studentContactEmail",
            "studentContactPhone",
            "adminContactEmail",
            "adminContactPhone",
        ]

    def validate_category_key(self, value):
        if not ClassCategory.objects.filter(key=value).exists():
            raise serializers.ValidationError(f"Category with key '{value}' not found.")
        return value

    def validate(self, data):
        category_key = data.get("category_key")
        subcategory_key = data.get("subcategory_key")

        if subcategory_key and category_key:
            category = ClassCategory.objects.filter(key=category_key).first()
            if (
                category
                and not ClassSubcategory.objects.filter(
                    category=category, key=subcategory_key
                ).exists()
            ):
                raise serializers.ValidationError(
                    {
                        "subcategory_key": f"Subcategory '{subcategory_key}' not found in category '{category.name}'."
                    }
                )
        return data

    def create(self, validated_data):
        """
        Custom create method to handle conversion of category/subcategory keys
        and to parse coordinates into latitude and longitude.
        """
        category_key = validated_data.pop("category_key")
        subcategory_key = validated_data.pop("subcategory_key", None)

        coordinates_str = validated_data.get("coordinates")
        if coordinates_str:
            try:
                lat_str, lng_str = map(str.strip, coordinates_str.split(","))
                validated_data["latitude"] = Decimal(lat_str)
                validated_data["longitude"] = Decimal(lng_str)
            except (ValueError, InvalidOperation):
                logger.warning(
                    f"Could not parse coordinates on create: '{coordinates_str}'. Lat/Lng will not be set."
                )
                validated_data["latitude"] = None
                validated_data["longitude"] = None
        else:
            validated_data["latitude"] = None
            validated_data["longitude"] = None

        try:
            category = ClassCategory.objects.get(key=category_key)
        except ClassCategory.DoesNotExist:
            raise serializers.ValidationError(
                {"category_key": f"Invalid category key: {category_key}"}
            )

        subcategory = None
        if subcategory_key:
            try:
                subcategory = ClassSubcategory.objects.get(
                    category=category, key=subcategory_key
                )
            except ClassSubcategory.DoesNotExist:
                raise serializers.ValidationError(
                    {
                        "subcategory_key": f"Invalid subcategory key '{subcategory_key}' for the selected category."
                    }
                )

        validated_data["category"] = category
        validated_data["subcategory"] = subcategory

        instance = ClassesMain.objects.create(**validated_data)
        return instance
