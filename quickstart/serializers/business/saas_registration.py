"""Slim SaaS business registration — name, website, phone, terms only."""

from urllib.parse import urlparse
import logging
import re

from django.core.validators import URLValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError

from quickstart.models import BusinessInfo, Role

logger = logging.getLogger(__name__)


def normalize_website(value):
    if not value:
        return value
    raw = str(value).strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = f"https://{raw}"
    validator = URLValidator()
    try:
        validator(raw)
    except DjangoValidationError:
        raise DRFValidationError("Enter a valid website URL.")
    return raw


def origin_from_website(url):
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".lower()
    return None


class BusinessRegistrationSerializer(serializers.ModelSerializer):
    website = serializers.CharField(required=True, allow_blank=False)
    studentContactPhone = serializers.CharField(required=True)
    plan_id = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = BusinessInfo
        fields = [
            "businessName",
            "website",
            "studentContactPhone",
            "termsAccepted",
            "privacyAccepted",
            "plan_id",
        ]
        extra_kwargs = {
            "businessName": {"required": True},
            "termsAccepted": {"required": True},
            "privacyAccepted": {"required": True},
        }

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

    def validate_website(self, value):
        return normalize_website(value)

    def validate(self, data):
        if not data.get("termsAccepted"):
            raise DRFValidationError(
                {"termsAccepted": "You must accept the Terms of Service"}
            )
        if not data.get("privacyAccepted"):
            raise DRFValidationError(
                {"privacyAccepted": "You must accept the Privacy Policy"}
            )
        plan_id = (data.get("plan_id") or "").strip().lower()
        if plan_id and plan_id not in ("basic", "growth", "advanced"):
            data["plan_id"] = "growth"
        return data

    def create(self, validated_data):
        user = self.context["request"].user
        validated_data.pop("plan_id", None)
        website = validated_data.get("website") or ""
        origin = origin_from_website(website)

        business = BusinessInfo.objects.create(
            owner=user,
            businessName=validated_data["businessName"],
            website=website or None,
            studentContactPhone=validated_data["studentContactPhone"],
            studentContactEmail=user.email or "",
            preferredContact="email",
            businessType="individual",
            businessDescription="",
            businessAddress="",
            businessCity="",
            businessState="",
            businessZipCode="",
            businessHours=[],
            business_timezone="America/Toronto",
            liabilityWaiver=False,
            termsAccepted=True,
            privacyAccepted=True,
            verificationStatus="verified",
            isActive=False,
            onboarding_completed=False,
            allowed_widget_origins=[origin] if origin else [],
            contact_privacy="on_booking",
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
        except Role.DoesNotExist:
            logger.warning(
                "Role '%s' not found. Cannot assign to user %s.",
                BUSINESS_OWNER_ROLE_NAME,
                user.email,
            )
        except Exception:
            logger.exception("Error assigning role to %s", user.email)

        return business
