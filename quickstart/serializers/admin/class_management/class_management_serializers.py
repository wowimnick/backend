import os
from django.conf import settings
from rest_framework import serializers
from decimal import Decimal  # Ensure Decimal is imported

from quickstart.utils.url_utils import build_cloudfront_url
from ....models import (
    ClassCategory,
    ClassCollection,
    ClassSubcategory,
    ClassesMain,
    ClassImage,
    ClassOption,
    Reviews,
    Schedule,
    ScheduleInstance,
    BusinessInfo,
)


# --- AdminScheduleInstanceSerializer ---
class AdminScheduleInstanceSerializer(serializers.ModelSerializer):
    booking_count = serializers.IntegerField(source="current_bookings", read_only=True)
    available_spots = serializers.IntegerField(read_only=True)

    class Meta:
        model = ScheduleInstance
        fields = [
            "id",
            "date",
            "time",
            "duration",
            "price",
            "max_participants",
            "status",
            "booking_count",
            "available_spots",
            "cancellation_reason",
            "created_at",
            "updated_at",
        ]


# --- AdminScheduleSerializer ---
class AdminScheduleSerializer(serializers.ModelSerializer):
    instances = AdminScheduleInstanceSerializer(many=True, read_only=True)

    class Meta:
        model = Schedule
        fields = [
            "id",
            "day",
            "time",
            "duration",
            "price",
            "maxParticipants",
            "start_date",
            "end_date",
            "date",
            "allow_late_enrollment",
            "instances",
        ]


# --- AdminClassOptionSerializer ---
class AdminClassOptionSerializer(serializers.ModelSerializer):
    schedules = AdminScheduleSerializer(many=True, read_only=True)
    price_range = serializers.SerializerMethodField()
    total_students = serializers.IntegerField(read_only=True, default=0)
    parent_class_title = serializers.CharField(source="classId.title", read_only=True)

    class Meta:
        model = ClassOption
        fields = [
            "optionId",
            "parent_class_title",
            "booking_type",
            "level",
            "price_type",
            "equipment",
            "tags",
            "cancellationPolicy",
            "cancellationCustomHours",
            "cancellationRefundPercentage",
            "schedules",
            "price_range",
            "total_students",
            "createdAt",
            "updatedAt",
        ]
        read_only_fields = [
            "optionId",
            "parent_class_title",
            "price_range",
            "total_students",
            "createdAt",
            "updatedAt",
        ]

    def get_price_range(self, obj):
        # In a real scenario, this should likely filter on active schedules
        # but for now, we'll keep it simple as per the original.
        all_schedules = obj.schedules.all()
        if not all_schedules:
            return None

        prices = [
            schedule.price for schedule in all_schedules if schedule.price is not None
        ]
        if not prices:
            return None

        min_price = min(prices)
        max_price = max(prices)

        if min_price == max_price:
            return {"min": min_price, "max": min_price, "single_price": True}

        return {"min": min_price, "max": max_price, "single_price": False}


# --- AdminClassImageSerializer ---
class AdminClassImageSerializer(serializers.ModelSerializer):
    """Provides multiple, optimized, absolute URLs for class images."""

    image_thumb_url = serializers.SerializerMethodField()
    image_medium_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassImage
        fields = ["imageId", "image_thumb_url", "image_medium_url", "createdAt"]

    def _get_resized_url(self, obj, size):
        if not obj.image or not hasattr(obj.image, "name") or not obj.image.name:
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None

        original_path = obj.image.name
        if not original_path.startswith("originals/"):
            return None

        base_path, _ = os.path.splitext(original_path)
        resized_base_path = base_path.replace("originals/", f"public/{size}/", 1)
        webp_path = resized_base_path + ".webp"

        return build_cloudfront_url(webp_path)

    def get_image_thumb_url(self, obj):
        return self._get_resized_url(obj, "thumb")

    def get_image_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")


class AdminBusinessSerializer(serializers.ModelSerializer):
    business_image_thumb_url = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            "businessId",
            "businessName",
            "businessType",
            "business_image_thumb_url",  # Corrected field
            "businessCity",
            "businessState",
            "verificationStatus",
            "isActive",
            "featured",
        ]

    def get_business_image_thumb_url(self, obj):
        if (
            not obj.businessImage
            or not hasattr(obj.businessImage, "name")
            or not obj.businessImage.name
        ):
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None

        original_path = obj.businessImage.name
        if not original_path.startswith("originals/"):
            return None

        base_path, _ = os.path.splitext(original_path)
        resized_base_path = base_path.replace("originals/", "public/thumb/", 1)
        webp_path = resized_base_path + ".webp"

        return build_cloudfront_url(webp_path)

class SimpleCollectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassCollection
        fields = ['id', 'name']

class AdminClassSerializer(serializers.ModelSerializer):
    images = AdminClassImageSerializer(many=True, read_only=True)
    business = AdminBusinessSerializer(source="businessId", read_only=True)
    businessId = serializers.IntegerField(source="businessId.pk", read_only=True)
    collections = SimpleCollectionSerializer(many=True, read_only=True)
    business_name = serializers.CharField(
        read_only=True
    )  # Will be populated by the annotation in the view
    business_featured = serializers.BooleanField(read_only=True, default=False)
    average_rating = serializers.FloatField(read_only=True, default=0.0)
    review_count = serializers.IntegerField(read_only=True, default=0)
    active_schedules_count = serializers.IntegerField(read_only=True, default=0)
    min_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, allow_null=True
    )
    max_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, allow_null=True
    )
    price_range = serializers.SerializerMethodField()
    platform_revenue = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, default=Decimal("0.00")
    )

    class Meta:
        model = ClassesMain
        fields = [
            "classId",
            "title",
            "description",
            "location",
            "min_price",
            "max_price", 
            "price_range",
            "status",
            "active_schedules_count",
            "business_featured",
            "images",
            "business",
            "businessId",
            "business_name",  
            "average_rating",
            "review_count",
            "platform_revenue",
            "createdAt",
            "updatedAt",
            "collections", 
        ]
        read_only_fields = fields

    def get_price_range(self, obj):
        min_price = getattr(obj, "min_price", None)
        max_price = getattr(obj, "max_price", None)

        # Fallback logic if annotations are None (requires prefetch of options__schedules)
        if min_price is None and max_price is None:
            prices = []
            # This fallback is inefficient and should be avoided by ensuring annotations are always present
            if hasattr(obj, "options"):
                for (
                    option
                ) in (
                    obj.options.all()
                ):  # Assumes prefetch_related('options__schedules')
                    for schedule in option.schedules.all():
                        if schedule.price is not None:
                            prices.append(schedule.price)
                if not prices:
                    return None
                min_price = min(prices)
                max_price = max(prices)
        elif min_price is None:  # Handle only max exists
            min_price = max_price
        elif max_price is None:  # Handle only min exists
            max_price = min_price

        if min_price is None:
            return None  # Still no price found

        min_price_dec = Decimal(str(min_price))
        max_price_dec = Decimal(str(max_price))

        if min_price_dec == max_price_dec:
            return {"min": min_price_dec, "max": min_price_dec, "single_price": True}
        else:
            return {"min": min_price_dec, "max": max_price_dec, "single_price": False}

    # --- AdminReviewSerializer ---
class AdminReviewSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    className = serializers.CharField(source="classId.title", read_only=True)
    businessName = serializers.CharField(
        source="businessId.businessName", read_only=True
    )

    class Meta:
        model = Reviews
        fields = [
            "reviewId",
            "userId",
            "user",
            "classId",
            "className",
            "businessId",
            "businessName",
            "rating",
            "comment",
            "status",
            "reported",
            "report_reason",
            "business_response",
            "createdAt",
        ]
        read_only_fields = [
            "reviewId",
            "userId",
            "user",
            "classId",
            "className",
            "businessId",
            "businessName",
            "createdAt",
            "rating",
            "comment",
        ]  # Define fields admin cannot edit directly

    def get_user(self, obj):
        if not obj.userId:
            return None

        full_name = f"{obj.userId.first_name} {obj.userId.last_name}".strip()
        avatar_url = None

        if (
            obj.userId.avatar
            and hasattr(obj.userId.avatar, "name")
            and obj.userId.avatar.name
            and getattr(settings, "CLOUDFRONT_DOMAIN", None)
        ):
            original_path = obj.userId.avatar.name
            if original_path.startswith("originals/"):
                base_path, _ = os.path.splitext(original_path)
                resized_base_path = base_path.replace("originals/", "public/thumb/", 1)
                webp_path = resized_base_path + ".webp"
                avatar_url = build_cloudfront_url(webp_path)

        return {
            "id": obj.userId.userId,
            "name": full_name or obj.userId.email,
            "avatar_thumb_url": avatar_url,
        }


# --- AdminClassDetailSerializer ---
class AdminClassDetailSerializer(AdminClassSerializer):
    """
    Detailed serializer for single class view in admin panel.
    Inherits fields from AdminClassSerializer and adds options/reviews.
    """

    options = AdminClassOptionSerializer(many=True, read_only=True)
    reviews = AdminReviewSerializer(many=True, read_only=True)

    class Meta(AdminClassSerializer.Meta):
        fields = list(AdminClassSerializer.Meta.fields) + [
            "options",
            "reviews",  # Keep 'reviews' here
            "studentContactEmail",
            "studentContactPhone",
            "features",
            "unit_number",
            "coordinates",
            "saltLocation",
            "city",
            "state",
        ]
        # Read-only fields are inherited, add new ones if needed
        read_only_fields = fields  # Keep detail view read-only for now


# --- AdminClassCreateSerializer ---
class AdminClassCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating/updating classes through admin panel
    """

    class Meta:
        model = ClassesMain
        fields = [
            "title",
            "description",
            "location",
            "coordinates",
            "status",
            "studentContactEmail",
            "studentContactPhone",
            # Add 'businessId' if admin needs to specify it during creation
        ]
        # Add write_only=True for fields like businessId if needed


# --- SubcategorySerializer ---
class SubcategorySerializer(serializers.ModelSerializer):
    class_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ClassSubcategory
        fields = ["id", "name", "key", "description", "class_count"]

class AdminClassCollectionSerializer(serializers.ModelSerializer):
    """
    Serializer for managing Class Collections (Vibes).
    Handles S3 image key updates and Automation Rules.
    """
    class_count = serializers.IntegerField(read_only=True, default=0)
    image_medium_url = serializers.SerializerMethodField()
    image_s3_key = serializers.CharField(write_only=True, required=False, allow_null=True)
    
    # Ensure automation_rules is treated as a Dict
    automation_rules = serializers.JSONField(required=False, default=dict)

    class Meta:
        model = ClassCollection
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "type",              # Added support for 'manual' vs 'automated'
            "automation_rules",  # Added support for JSON rules
            "image_medium_url",
            "image_s3_key",
            "is_active",
            "sort_order",
            "class_count", 
        ]
        read_only_fields = ["id", "image_medium_url", "class_count"]

    def get_image_medium_url(self, obj):
        if not obj.image or not hasattr(obj.image, "name") or not obj.image.name:
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None

        original_path = obj.image.name
        if not original_path.startswith("originals/"):
            return None

        base_name, _ = os.path.splitext(original_path.replace("originals/", "", 1))
        path = f"public/medium/{base_name}.webp"
        return build_cloudfront_url(path)

    def _handle_image_update(self, instance, s3_key_data):
        s3_key = s3_key_data.pop("image_s3_key", "NOT_PROVIDED")
        if s3_key is None:
            if instance.image:
                instance.image.delete(save=False)
            instance.image = None
        elif s3_key != "NOT_PROVIDED":
            if instance.image:
                instance.image.delete(save=False)
            instance.image = s3_key

    def create(self, validated_data):
        s3_key = validated_data.pop("image_s3_key", None)
        instance = ClassCollection.objects.create(**validated_data)
        if s3_key:
            instance.image = s3_key
            instance.save()
        return instance

    def update(self, instance, validated_data):
        self._handle_image_update(instance, validated_data)
        return super().update(instance, validated_data)
    
# --- AdminClassCategorySerializer ---
class AdminClassCategorySerializer(serializers.ModelSerializer):
    """
    Handles serialization for the admin category management view.
    MODIFIED to correctly handle image updates via an S3 key instead of direct upload.
    """

    subcategories = SubcategorySerializer(many=True, read_only=True)
    activeClasses = serializers.IntegerField(
        read_only=True, default=0, source="active_classes"
    )
    class_count = serializers.IntegerField(read_only=True, default=0)
    image_medium_url = serializers.SerializerMethodField()

    # MODIFIED: This field now accepts an S3 key from the frontend.
    image_s3_key = serializers.CharField(
        write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = ClassCategory
        fields = [
            "id",
            "name",
            "key",
            "description",
            "image_s3_key",
            "sort_order",
            "image_medium_url",
            "color",
            "icon_name",
            "created_at",
            "updated_at",
            "subcategories",
            "activeClasses",
            "class_count",
        ]
        read_only_fields = [
            "id",
            "key",
            "created_at",
            "updated_at",
            "subcategories",
            "activeClasses",
            "class_count",
            "image_medium_url",
        ]

    def get_image_medium_url(self, obj):
        if not obj.image or not hasattr(obj.image, "name") or not obj.image.name:
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None

        original_path = obj.image.name
        if not original_path.startswith("originals/"):
            return None

        # Correctly generate the .webp path for the medium size
        base_name, _ = os.path.splitext(original_path.replace("originals/", "", 1))
        webp_path = f"public/medium/{base_name}.webp"

        return build_cloudfront_url(webp_path)

    def _handle_image_update(self, instance, s3_key_data):
        """Helper to process image updates from an S3 key."""
        s3_key = s3_key_data.pop("image_s3_key", "NOT_PROVIDED")

        # If null was sent, clear the image.
        if s3_key is None:
            if instance.image:
                instance.image.delete(save=False)
            instance.image = None
        # If a new key was provided, update it.
        elif s3_key != "NOT_PROVIDED":
            if instance.image:
                instance.image.delete(save=False)  # Delete old S3 object
            instance.image = s3_key  # Assign the new S3 key string

    def create(self, validated_data):
        s3_key = validated_data.pop("image_s3_key", None)
        instance = ClassCategory.objects.create(**validated_data)
        if s3_key:
            instance.image = s3_key
            instance.save()
        return instance

    def update(self, instance, validated_data):
        self._handle_image_update(instance, validated_data)
        return super().update(instance, validated_data)


class ReassignmentSerializer(serializers.Serializer):
    new_id = serializers.IntegerField(required=True)

    def validate_new_id(self, value):
        if not isinstance(value, int):
            raise serializers.ValidationError("A valid integer ID is required.")
        return value
