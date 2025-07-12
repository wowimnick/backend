import re
import string
from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email, URLValidator, RegexValidator
from rest_framework.exceptions import ValidationError as DRFValidationError
from decimal import Decimal, InvalidOperation
import json
import logging
import pytz  # For timezone choices

from quickstart.models import (
    BusinessInfo,
    ClassCategory,
    ClassesMain,
    Reviews,
    CustomUser,
    Booking,
    VerificationRequest,
    Role,
)  # Added Role
from django.db.models import Sum, Count, Avg, Q, Subquery, OuterRef, IntegerField, F
from django.db.models.functions import Coalesce
from datetime import (
    timedelta,
    time,
    datetime,
)  # Added datetime for founding_year validation
from django.utils import timezone

logger = logging.getLogger(__name__)

colors = {
    "chart": {
        "blue": "#3b82f6",
        "green": "#10b981",
        "purple": "#8b5cf6",
        "orange": "#f97316",
        "red": "#ef4444",
    }
}

COMMON_TIMEZONE_CHOICES_SERIALIZER = [
    (tz, tz.replace("_", " ")) for tz in pytz.common_timezones
]


class MetricSerializer(serializers.Serializer):
    value = serializers.FloatField()
    change = serializers.FloatField()


class RevenueTrendItemSerializer(serializers.Serializer):
    date = serializers.CharField()
    gross_revenue = serializers.FloatField(required=False, default=0.0)
    platform_fees = serializers.FloatField(required=False, default=0.0)
    net_revenue = serializers.FloatField(required=False, default=0.0)


class UpcomingClassSerializer(serializers.Serializer):
    schedule_instance_id = serializers.IntegerField(required=False)
    name = serializers.CharField()
    time = serializers.CharField()
    current_occupancy = serializers.IntegerField()
    max_occupancy = serializers.IntegerField()


class PopularClassSerializer(serializers.Serializer):
    name = serializers.CharField()
    enrollment = serializers.IntegerField()


class RecentActivitySerializer(serializers.Serializer):
    message = serializers.CharField()
    icon = serializers.CharField()
    color = serializers.CharField()
    timestamp = serializers.DateTimeField(read_only=True)


class MetricsContainerSerializer(serializers.Serializer):
    total_students = MetricSerializer()
    active_classes = MetricSerializer()
    monthly_revenue = MetricSerializer()
    average_rating = MetricSerializer()


class SetupProgressSerializer(serializers.Serializer):
    is_stripe_connected = serializers.BooleanField()
    is_profile_complete = serializers.BooleanField()
    has_created_class = serializers.BooleanField()
    has_class_options = serializers.BooleanField()
    has_schedules = serializers.BooleanField()


class BusinessDashboardOverviewSerializer(serializers.Serializer):
    metrics = MetricsContainerSerializer()
    revenue_trend = RevenueTrendItemSerializer(many=True)
    upcoming_classes = UpcomingClassSerializer(many=True)
    popular_classes = PopularClassSerializer(many=True)
    recent_activity = RecentActivitySerializer(many=True)
    today_snapshot = serializers.DictField(
        child=serializers.IntegerField(), required=False
    )
    actionable_prompts = serializers.DictField(required=False)
    setup_progress = SetupProgressSerializer(required=False)


class BusinessRegistrationSerializer(serializers.ModelSerializer):
    latitude = serializers.DecimalField(
        max_digits=10, decimal_places=8, required=False, allow_null=True
    )
    longitude = serializers.DecimalField(
        max_digits=11, decimal_places=8, required=False, allow_null=True
    )

    classFormats = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    skillLevels = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )
    ageGroups = serializers.CharField(write_only=True, required=False, allow_blank=True)

    website = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    business_timezone = serializers.ChoiceField(
        choices=COMMON_TIMEZONE_CHOICES_SERIALIZER, required=True
    )
    social_media_links = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="JSON string of social media links, e.g. {'facebook':'url'}",
    )
    tags_keywords = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="JSON string of a list of keywords",
    )
    founding_year = serializers.IntegerField(required=False, allow_null=True)

    studentContactPhone = serializers.CharField(required=True)

    class Meta:
        model = BusinessInfo
        fields = [
            # Step 0: Business Info
            "businessName",
            "businessType",
            "businessDescription",
            "businessImage",
            "openingTime",
            "closingTime",
            "liabilityWaiver",
            "website",
            "business_timezone",
            "social_media_links",
            "tags_keywords",
            "founding_year",
            # Step 1: Contact Details
            "studentContactPhone",
            "studentContactEmail",
            "preferredContact",
            "contact_privacy",
            # Step 2: Location
            "businessAddress",
            "businessCity",
            "businessState",
            "businessZipCode",
            "latitude",
            "longitude",
            "showExactLocation",
            # Step 3: Class Types & Agreements
            "classFormats",
            "skillLevels",
            "ageGroups",
            "termsAccepted",
            "privacyAccepted",
        ]
        extra_kwargs = {
            "businessName": {"required": True},
            "businessType": {"required": True},
            "businessDescription": {
                "required": True,
                "min_length": 250,
                "max_length": 750,
            },
            "openingTime": {"required": True},
            "closingTime": {"required": True},
            "liabilityWaiver": {"required": True},
            "businessImage": {"required": False, "allow_null": True},
            "website": {"required": False, "allow_blank": True, "allow_null": True},
            "business_timezone": {"required": True},
            "studentContactEmail": {"required": True},
            "preferredContact": {"required": True},
            "contact_privacy": {"required": True},
            "businessAddress": {"required": True},
            "businessCity": {"required": True},
            "businessState": {"required": True},
            "businessZipCode": {"required": True},
            "termsAccepted": {"required": True},
            "privacyAccepted": {"required": True},
            "showExactLocation": {"read_only": True},
        }

    def validate_website(self, value):
        if value:
            if not all(
                c in string.ascii_letters + string.digits + "-._~:/?#[]@!$&'()*+,;="
                for c in value.replace("https://", "").replace("http://", "")
            ):
                raise DRFValidationError("Website URL contains invalid characters.")
            validator = URLValidator()
            try:
                validator(value)
            except DjangoValidationError:
                raise DRFValidationError("Invalid URL format for website.")
        return value

    def validate_studentContactPhone(self, value):
        cleaned_number = "".join(filter(str.isdigit, value))
        if value.startswith("+"):
            cleaned_number = "+" + cleaned_number

        phone_regex = r"^\+?[1-9]\d{1,14}$"
        if not re.match(phone_regex, cleaned_number):
            raise DRFValidationError(
                "Please enter a valid phone number, including country code if applicable."
            )
        return cleaned_number

    def validate_founding_year(self, value):
        if value is not None:
            current_year = datetime.now().year
            if not (1800 <= value <= current_year):
                raise DRFValidationError(
                    f"Founding year must be between 1800 and {current_year}."
                )
        return value

    def validate(self, data):
        if data.get("studentContactEmail"):
            try:
                validate_email(data["studentContactEmail"])
            except DjangoValidationError:
                raise DRFValidationError(
                    {"studentContactEmail": "Invalid email address"}
                )

        if data.get("openingTime") and data.get("closingTime"):
            if data["openingTime"] >= data["closingTime"]:
                raise DRFValidationError(
                    {"closingTime": "Closing time must be after opening time"}
                )

        if not data.get("termsAccepted"):
            raise DRFValidationError(
                {"termsAccepted": "You must accept the Terms of Service"}
            )
        if not data.get("privacyAccepted"):
            raise DRFValidationError(
                {"privacyAccepted": "You must accept the Privacy Policy"}
            )

        liability_waiver = data.get("liabilityWaiver")
        if isinstance(liability_waiver, str):
            data["liabilityWaiver"] = liability_waiver.lower() == "true"
        elif not isinstance(liability_waiver, bool):
            raise DRFValidationError(
                {"liabilityWaiver": "Invalid value for liability waiver."}
            )
        if not data.get("liabilityWaiver"):
            raise DRFValidationError(
                {"liabilityWaiver": "You must agree to the liability waiver."}
            )

        latitude = data.get("latitude")
        longitude = data.get("longitude")
        if (latitude is not None and longitude is None) or (
            longitude is not None and latitude is None
        ):
            raise DRFValidationError(
                "Both latitude and longitude must be provided together, or neither."
            )
        data["showExactLocation"] = latitude is not None and longitude is not None

        json_string_fields = {
            "classFormats": list,
            "skillLevels": list,
            "ageGroups": list,
            "tags_keywords": list,
            "social_media_links": dict,
        }
        for field, expected_type in json_string_fields.items():
            field_value = data.get(field)
            if field_value and isinstance(field_value, str):
                try:
                    parsed_value = json.loads(field_value)
                    if not isinstance(parsed_value, expected_type):
                        raise DRFValidationError(
                            {
                                field: f"Invalid format. Expected a {expected_type.__name__}."
                            }
                        )
                    data[field] = parsed_value
                except json.JSONDecodeError:
                    raise DRFValidationError(
                        {field: f"Invalid JSON format for {field}."}
                    )
            elif field in data and not field_value:
                data[field] = expected_type()

        data["isActive"] = False
        return data

    def create(self, validated_data):
        user = self.context["request"].user

        # Pop list/dict fields to handle them separately
        classformats_data = validated_data.pop("classFormats", [])
        skilllevels_data = validated_data.pop("skillLevels", [])
        agegroups_data = validated_data.pop("ageGroups", [])
        tags_keywords_data = validated_data.pop("tags_keywords", [])
        social_media_data = validated_data.pop("social_media_links", {})

        business = BusinessInfo.objects.create(
            owner=user,
            verificationStatus="pending",
            classFormats=classformats_data,
            skillLevels=skilllevels_data,
            ageGroups=agegroups_data,
            tags_keywords=tags_keywords_data,
            social_media_links=social_media_data,
            **validated_data,
        )

        VerificationRequest.objects.create(
            user=user, business=business, status="pending"
        )

        BUSINESS_OWNER_ROLE_NAME = "Business Owner"
        try:
            target_role = Role.objects.get(name=BUSINESS_OWNER_ROLE_NAME)
            if (
                user.role is None
                or target_role.hierarchy_level > user.role.hierarchy_level
            ):
                user.role = target_role
                user.save(update_fields=["role"])
                logger.info(f"User {user.email} assigned role '{target_role.name}'.")
        except Role.DoesNotExist:
            logger.warning(
                f"Role '{BUSINESS_OWNER_ROLE_NAME}' not found. Cannot assign to user {user.email}."
            )
        except Exception as e:
            logger.error(
                f"Error assigning role to {user.email}: {str(e)}", exc_info=True
            )

        return business


class ManagedBusinessInfoSerializer(serializers.ModelSerializer):
    owner_email = serializers.EmailField(source="owner.email", read_only=True)
    managers_emails = serializers.SerializerMethodField(read_only=True)
    businessImage = serializers.ImageField(
        required=False, allow_null=True, use_url=True
    )
    website = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    business_timezone = serializers.ChoiceField(
        choices=COMMON_TIMEZONE_CHOICES_SERIALIZER, required=False, allow_blank=True
    )
    social_media_links = serializers.JSONField(required=False, allow_null=True)
    tags_keywords = serializers.JSONField(required=False, allow_null=True)
    founding_year = serializers.IntegerField(required=False, allow_null=True)
    latitude = serializers.DecimalField(
        max_digits=10, decimal_places=8, required=False, allow_null=True
    )
    longitude = serializers.DecimalField(
        max_digits=11, decimal_places=8, required=False, allow_null=True
    )
    totalReviews = serializers.IntegerField(
        source="total_reviews_count", read_only=True
    )
    average_rating = serializers.DecimalField(
        max_digits=3, decimal_places=1, read_only=True
    )

    classFormats = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False
    )
    skillLevels = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False
    )
    ageGroups = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False
    )

    class Meta:
        model = BusinessInfo
        fields = [
            "businessId",
            "businessName",
            "businessType",
            "businessDescription",
            "businessImage",
            "website",
            "business_timezone",
            "social_media_links",
            "tags_keywords",
            "founding_year",
            "studentContactEmail",
            "studentContactPhone",
            "businessAddress",
            "latitude",
            "longitude",
            "showExactLocation",
            "openingTime",
            "closingTime",
            "contact_privacy",
            "preferredContact",
            "newBookingNotification",
            "cancellationNotification",
            "reminderNotification",
            "smsNotifications",
            "liabilityWaiver",
            "classFormats",
            "skillLevels",
            "ageGroups",
            "owner_email",
            "managers_emails",
            "verificationStatus",
            "stripe_account_id",
            "stripe_account_status",
            "createdAt",
            "updatedAt",
            "average_rating",
            "totalReviews",
        ]
        read_only_fields = (
            "businessId",
            "owner_email",
            "managers_emails",
            "verificationStatus",
            "stripe_account_id",
            "stripe_account_status",
            "createdAt",
            "updatedAt",
            "average_rating",
            "totalReviews",
            "classFormats",
            "skillLevels",
            "ageGroups",
        )
        extra_kwargs = {
            "businessName": {"required": False},
            "businessType": {"required": False},
            "contact_privacy": {"required": False},
            "businessDescription": {"required": False, "allow_blank": True},
            "studentContactEmail": {"required": False, "allow_blank": True},
            "studentContactPhone": {"required": False, "allow_blank": True},
            "website": {"required": False, "allow_blank": True, "allow_null": True},
            "business_timezone": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
            "social_media_links": {"required": False, "allow_null": True},
            "tags_keywords": {"required": False, "allow_null": True},
            "founding_year": {"required": False, "allow_null": True},
            "businessAddress": {"required": False, "allow_blank": True},
            "latitude": {"required": False, "allow_null": True},
            "longitude": {"required": False, "allow_null": True},
            "openingTime": {"required": False, "allow_null": True},
            "closingTime": {"required": False, "allow_null": True},
            "preferredContact": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
            "liabilityWaiver": {"required": False},
            "showExactLocation": {"required": False},
            "newBookingNotification": {"required": False},
            "cancellationNotification": {"required": False},
            "reminderNotification": {"required": False},
            "smsNotifications": {"required": False},
        }

    def get_managers_emails(self, obj):
        if hasattr(obj, "managers"):
            return [manager.email for manager in obj.managers.all()]
        return []

    def _parse_boolean_from_string(self, value, field_name):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.lower() == "true":
                return True
            if value.lower() == "false":
                return False
        raise DRFValidationError({field_name: f"Invalid boolean value: '{value}'."})

    def _parse_json_from_string(self, value, field_name, expected_type=None):
        logger.debug(
            f"Parsing JSON for {field_name}, value: '{value}', type: {type(value)}"
        )

        # Case 1: Value is already a Python list/dict (e.g., from JSONParser if not FormData)
        if isinstance(value, (list, dict)):
            if expected_type and not isinstance(value, expected_type):
                logger.warning(
                    f"Pre-parsed JSON for {field_name} has unexpected type. Expected {expected_type.__name__}, got {type(value).__name__}."
                )
                raise DRFValidationError(
                    {
                        field_name: f"Invalid pre-parsed format. Expected a {expected_type.__name__} but got {type(value).__name__}."
                    }
                )
            logger.debug(f"Value for {field_name} is already parsed: {value}")
            return value

        # Case 2: Value is a string (e.g., from FormData)
        if isinstance(value, str):
            if not value.strip():  # Empty or whitespace-only string
                logger.debug(
                    f"Empty string for {field_name}, defaulting to empty {expected_type or 'container'}."
                )
                return (
                    expected_type()
                    if expected_type is not None
                    else (None if value is None else {})
                )  # Default to dict if expected_type is None but not an empty string
            try:
                parsed_value = json.loads(value)
                if expected_type and not isinstance(parsed_value, expected_type):
                    logger.warning(
                        f"Parsed JSON for {field_name} has unexpected structure. Expected {expected_type.__name__}, got {type(parsed_value).__name__}."
                    )
                    raise DRFValidationError(
                        {
                            field_name: f"Invalid JSON structure. Expected {expected_type.__name__} but got {type(parsed_value).__name__}."
                        }
                    )
                logger.debug(
                    f"Successfully parsed string for {field_name}: {parsed_value}"
                )
                return parsed_value
            except json.JSONDecodeError:
                logger.warning(
                    f"JSONDecodeError for {field_name} with value: '{value}'"
                )
                raise DRFValidationError(
                    {field_name: f"Invalid JSON string provided for {field_name}."}
                )

        # Case 3: Value is None
        if value is None:
            logger.debug(
                f"None value for {field_name}, defaulting to empty {expected_type or 'container'}."
            )
            return expected_type() if expected_type is not None else None

        # Case 4: Value is some other unexpected type
        logger.error(
            f"Unexpected data type for {field_name}: {type(value).__name__}. Value: '{value}'"
        )
        raise DRFValidationError(
            {
                field_name: f"Unexpected data type for {field_name}: {type(value).__name__}. Expected a JSON string, list, dict, or None."
            }
        )

    def to_internal_value(self, data):
        # Convert QueryDict to a mutable Python dictionary FIRST
        if hasattr(data, "dict") and callable(
            data.dict
        ):  # QueryDict has a .dict() method
            processed_data = data.dict()
        elif isinstance(data, dict):
            import copy

            processed_data = copy.deepcopy(data)  # Or data.copy() if deep is not needed
        else:  # Fallback for other types, though less common here
            import copy

            processed_data = (
                data.copy() if hasattr(data, "copy") else copy.deepcopy(data)
            )

        parsing_errors = {}

        # Handle boolean string fields from FormData
        boolean_fields = [
            "liabilityWaiver",
            "showExactLocation",
            "newBookingNotification",
            "cancellationNotification",
            "reminderNotification",
            "smsNotifications",
        ]
        for field in boolean_fields:
            if field in processed_data:
                current_value = processed_data.get(field)
                if isinstance(current_value, str):
                    try:
                        # For standard dict, direct assignment is fine
                        processed_data[field] = self._parse_boolean_from_string(
                            current_value, field
                        )
                    except DRFValidationError as e:
                        parsing_errors.update(e.detail)
                elif current_value is None and self.fields[field].allow_null:
                    processed_data[field] = None

        json_fields_config = {
            "social_media_links": dict,
            "tags_keywords": list,
        }

        for field_name, expected_type in json_fields_config.items():
            if field_name in processed_data:
                current_value = processed_data.get(field_name)
                try:
                    # Direct assignment to the standard dictionary
                    processed_data[field_name] = self._parse_json_from_string(
                        current_value, field_name, expected_type
                    )
                except DRFValidationError as e:
                    parsing_errors.update(e.detail)
            elif self.fields[field_name].required:
                parsing_errors.setdefault(field_name, []).append(
                    "This field is required."
                )

        if "businessImage" in processed_data and processed_data["businessImage"] == "":
            processed_data["businessImage"] = None

        for coord_field in ["latitude", "longitude"]:
            if coord_field in processed_data:
                value = processed_data[coord_field]
                if value == "":
                    processed_data[coord_field] = None
                elif value is not None and not isinstance(value, Decimal):
                    try:
                        processed_data[coord_field] = Decimal(str(value))
                    except (InvalidOperation, ValueError, TypeError):
                        parsing_errors.setdefault(coord_field, []).append(
                            f"Invalid decimal value for {coord_field}."
                        )

        if parsing_errors:
            logger.warning(f"Parsing errors in to_internal_value: {parsing_errors}")
            raise DRFValidationError(parsing_errors)

        logger.info(f"Processed data for super call (now a dict): {processed_data}")
        return super().to_internal_value(processed_data)

    def validate_studentContactEmail(self, value):
        if value:  # Allow blank
            try:
                validate_email(value)
            except DjangoValidationError:
                raise DRFValidationError("Invalid business email address.")
        return value

    def validate_website(self, value):
        if value:  # Allow blank
            validator = URLValidator()
            try:
                validator(value)
            except DjangoValidationError:
                raise DRFValidationError("Invalid URL format for website.")
        return value

    def validate_founding_year(self, value):
        if value is not None:  # Allow null
            current_year = datetime.now().year
            if not isinstance(value, int) or not (1800 <= value <= current_year):
                raise DRFValidationError(
                    f"Founding year must be an integer between 1800 and {current_year}."
                )
        return value

    def validate_tags_keywords(self, value):
        if value is not None:  # Allow null
            if not isinstance(value, list):
                raise DRFValidationError("Tags/Keywords must be a list.")
            if not all(isinstance(item, str) for item in value):
                raise DRFValidationError("All tags/keywords must be strings.")
        return (
            value if value is not None else []
        )  # Ensure empty list if None was intended for list

    def validate_social_media_links(self, value):
        if value is not None:  # Allow null
            if not isinstance(value, dict):
                raise DRFValidationError("Social media links must be a dictionary.")
            url_validator = URLValidator()
            for key, url_val in value.items():
                if not isinstance(key, str) or not isinstance(url_val, str):
                    raise DRFValidationError(
                        f"Invalid format for social media link: {key}. Both key and URL must be strings."
                    )
                if url_val:  # Only validate if URL is not empty and not null
                    try:
                        url_validator(url_val)
                    except DjangoValidationError:
                        raise DRFValidationError(
                            f"Invalid URL for social media '{key}': {url_val}"
                        )
        return (
            value if value is not None else {}
        )  # Ensure empty dict if None was intended for dict

    def validate_openingTime(self, value):
        if isinstance(value, str):  # From FormData
            try:
                return time.fromisoformat(
                    value.split(".")[0]
                )  # Handle HH:MM:SS or HH:MM
            except ValueError:
                raise DRFValidationError(
                    "Invalid opening time format. Use HH:MM or HH:MM:SS."
                )
        return value  # Already a time object

    def validate_closingTime(self, value):
        if isinstance(value, str):  # From FormData
            try:
                return time.fromisoformat(value.split(".")[0])
            except ValueError:
                raise DRFValidationError(
                    "Invalid closing time format. Use HH:MM or HH:MM:SS."
                )
        return value

    def validate(self, data):
        # Retrieve instance if updating
        instance = getattr(self, "instance", None)

        opening_time_str = self.context["request"].data.get("openingTime")
        closing_time_str = self.context["request"].data.get("closingTime")

        # Get current or new values for opening and closing times
        opening = data.get("openingTime", instance.openingTime if instance else None)
        closing = data.get("closingTime", instance.closingTime if instance else None)

        # If times are provided as strings (e.g. from FormData and not yet converted by field validation)
        # This happens if validate_openingTime/validate_closingTime is not called or they pass string
        if isinstance(opening, str):
            try:
                opening = time.fromisoformat(opening.split(".")[0])
            except ValueError:
                pass  # Let field validation catch it if it hasn't already
        if isinstance(closing, str):
            try:
                closing = time.fromisoformat(closing.split(".")[0])
            except ValueError:
                pass

        if opening and closing and opening >= closing:
            raise DRFValidationError(
                {"closingTime": "Closing time must be after opening time."}
            )

        # Coordinate validation: if one is provided, the other must also be.
        latitude = data.get("latitude", instance.latitude if instance else None)
        longitude = data.get("longitude", instance.longitude if instance else None)

        if (latitude is not None and longitude is None) or (
            longitude is not None and latitude is None
        ):
            raise DRFValidationError(
                {
                    "coordinates": "Both latitude and longitude must be provided together, or neither."
                }
            )

        # Automatically set showExactLocation based on coordinates if not explicitly provided
        if "showExactLocation" not in data and (
            latitude is not None and longitude is not None
        ):
            data["showExactLocation"] = True
        elif "showExactLocation" not in data and (
            latitude is None and longitude is None
        ):
            # If coordinates are cleared, and showExactLocation is not sent, default it based on instance or to False
            data["showExactLocation"] = (
                instance.showExactLocation
                if instance and latitude is None and longitude is None
                else False
            )

        # If businessImage is explicitly set to None in data (meaning remove image)
        # and the instance currently has an image, this is valid.
        # If businessImage is not in data, it means no change to image.
        # If businessImage is a file, it's an upload.
        # This logic is mostly handled by how `update` method processes `businessImage`.

        return data

    def update(self, instance, validated_data):
        # Handle businessImage: if None, it means remove. If a file, update. If not in validated_data, no change.
        business_image_update = validated_data.pop(
            "businessImage", ...
        )  # Use a sentinel

        if business_image_update is None:  # Explicitly set to None means remove
            if instance.businessImage:
                instance.businessImage.delete(save=False)
            instance.businessImage = None
        elif business_image_update is not ...:  # A new file was provided
            if instance.businessImage:
                instance.businessImage.delete(
                    save=False
                )  # Delete old before saving new
            instance.businessImage = business_image_update

        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()
        return instance


class BusinessStatsSerializer(serializers.ModelSerializer):
    total_revenue = serializers.SerializerMethodField()
    total_students = serializers.SerializerMethodField()
    total_classes = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    recent_bookings = serializers.SerializerMethodField()
    registration_date = serializers.DateTimeField(source="createdAt", read_only=True)
    totalReviews = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            "businessId",
            "businessName",
            "businessImage",
            "totalReviews",
            "total_revenue",
            "total_students",
            "total_classes",
            "average_rating",
            "recent_bookings",
            "registration_date",
        ]

    def get_totalReviews(self, obj):
        # Assuming Reviews model has a ForeignKey to BusinessInfo named 'business' or similar
        # If Reviews are linked via ClassesMain -> BusinessInfo, adjust the query
        # Example: return Reviews.objects.filter(classId__businessId=obj, status='approved').count()
        return obj.reviews_directly_to_business.filter(
            status="approved"
        ).count()  # Use the direct relation if available

    def get_average_rating(self, obj):
        avg = (
            obj.reviews_directly_to_business.filter(status="approved").aggregate(
                avg=Avg("rating")
            )["avg"]
            or 0.0
        )
        return round(avg, 1)

    def get_total_revenue(self, obj):
        valid_booking_ids = (
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=obj,
                payment_status="paid",
            )
            .filter(
                Q(booking_group_id__isnull=True)
                | Q(
                    id=Subquery(
                        Booking.objects.filter(
                            booking_group_id=OuterRef("booking_group_id")
                        )
                        .order_by("id")
                        .values("id")[:1]
                    )
                )
            )
            .values_list("id", flat=True)
        )

        total = Booking.objects.filter(id__in=list(valid_booking_ids)).aggregate(
            total=Sum("amount_paid")
        )["total"] or Decimal("0.00")
        return float(total)

    def get_total_students(self, obj):
        return (
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=obj
            )
            .values("user")
            .distinct()
            .count()
        )

    def get_total_classes(self, obj):
        # Assuming ClassesMain has ForeignKey 'businessId' to BusinessInfo
        return ClassesMain.objects.filter(businessId=obj, status="active").count()

    def get_recent_bookings(self, obj):
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return (
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=obj,
                booking_date__gte=thirty_days_ago,
                payment_status="paid",
            )
            .filter(
                Q(booking_group_id__isnull=True)
                | Q(
                    id=Subquery(
                        Booking.objects.filter(
                            booking_group_id=OuterRef("booking_group_id")
                        )
                        .order_by("id")
                        .values("id")[:1]
                    )
                )
            )
            .count()
        )
