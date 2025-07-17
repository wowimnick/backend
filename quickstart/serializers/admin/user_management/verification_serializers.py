# quickstart/serializers/admin/user_management/verification_serializers.py

import os
from django.conf import settings
from rest_framework import serializers
from django.utils import timezone
from ....models import VerificationRequest, VerificationDocument


class VerificationDocumentSerializer(serializers.ModelSerializer):
    """Serializer for verification documents"""

    document_type_display = serializers.CharField(
        source="get_document_type_display", read_only=True
    )
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = VerificationDocument
        fields = [
            "id",
            "document_type",
            "document_type_display",
            "filename",
            "file_type",
            "file",
            "file_url",
            "uploaded_at",
        ]
        read_only_fields = ["id", "filename", "file_type", "uploaded_at"]

    def get_file_url(self, obj):
        if obj.file:
            return obj.file.url
        return None


class VerificationRequestListSerializer(serializers.ModelSerializer):
    """List serializer for verification requests"""

    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source="user.email")
    business_name = serializers.CharField(
        source="business.businessName", read_only=True
    )
    document_count = serializers.SerializerMethodField()
    role = serializers.CharField(source="user.role.name", read_only=True)
    role_color = serializers.CharField(source="user.role.color", read_only=True)
    user_avatar = serializers.SerializerMethodField()
    business_avatar = serializers.SerializerMethodField()
    reviewer_name = serializers.SerializerMethodField()

    class Meta:
        model = VerificationRequest
        fields = [
            "id",
            "user",
            "user_name",
            "user_email",
            "business",
            "business_name",
            "status",
            "submitted_at",
            "document_count",
            "role",
            "role_color",
            "user_avatar",
            "business_avatar",
            "reviewed_by",
            "reviewer_name",
            "notes",
            "rejection_reason",
            "reviewed_at",
            "updated_at",
        ]

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()

    def get_document_count(self, obj):
        return obj.documents.count()

    def get_user_avatar(self, obj):
        if (
            not obj.user
            or not obj.user.avatar
            or not hasattr(obj.user.avatar, "name")
            or not obj.user.avatar.name
        ):
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None
        original_path = obj.user.avatar.name
        if not original_path.startswith("originals/"):
            return None
        base_path, _ = os.path.splitext(original_path)
        resized_base_path = base_path.replace("originals/", "public/thumb/", 1)
        webp_path = resized_base_path + ".webp"
        return f"{settings.CLOUDFRONT_DOMAIN}/{webp_path}"

    def get_business_avatar(self, obj):
        if (
            not obj.business
            or not obj.business.businessImage
            or not hasattr(obj.business.businessImage, "name")
            or not obj.business.businessImage.name
        ):
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None
        original_path = obj.business.businessImage.name
        if not original_path.startswith("originals/"):
            return None
        base_path, _ = os.path.splitext(original_path)
        resized_base_path = base_path.replace("originals/", "public/thumb/", 1)
        webp_path = resized_base_path + ".webp"
        return f"{settings.CLOUDFRONT_DOMAIN}/{webp_path}"

    def get_reviewer_name(self, obj):
        if obj.reviewed_by:
            return f"{obj.reviewed_by.first_name} {obj.reviewed_by.last_name}".strip()
        return None


class VerificationRequestDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for verification requests. Leverages the existing relationship to BusinessInfo."""

    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_avatar = serializers.SerializerMethodField()

    # --- Comprehensive Business Information ---
    business_name = serializers.CharField(
        source="business.businessName", read_only=True, default=""
    )
    business_image_medium_url = serializers.SerializerMethodField()
    business_type = serializers.CharField(
        source="business.businessType", read_only=True, default=""
    )
    business_description = serializers.CharField(
        source="business.businessDescription", read_only=True, default=""
    )
    business_address = serializers.CharField(
        source="business.businessAddress", read_only=True, default=""
    )
    business_city = serializers.CharField(
        source="business.businessCity", read_only=True, default=""
    )
    business_state = serializers.CharField(
        source="business.businessState", read_only=True, default=""
    )
    business_zip = serializers.CharField(
        source="business.businessZipCode", read_only=True, default=""
    )
    business_contact_email = serializers.EmailField(
        source="business.studentContactEmail", read_only=True, default=""
    )
    business_contact_phone = serializers.CharField(
        source="business.studentContactPhone", read_only=True, default=""
    )
    business_website = serializers.URLField(
        source="business.website", read_only=True, default="", allow_null=True
    )
    social_media_links = serializers.JSONField(
        source="business.social_media_links", read_only=True, default=dict
    )

    reviewer_name = serializers.SerializerMethodField()
    role = serializers.CharField(source="user.role.name", read_only=True)
    role_color = serializers.CharField(source="user.role.color", read_only=True)

    class Meta:
        model = VerificationRequest
        fields = [
            "id",
            "user",
            "user_name",
            "user_email",
            "user_avatar",
            "business",
            "business_name",
            "business_image_medium_url",
            "business_type",
            "business_description",  # <<< CHANGED
            "business_address",
            "business_city",
            "business_state",
            "business_zip",
            "business_contact_email",
            "business_contact_phone",
            "business_website",
            "social_media_links",
            "status",
            "submitted_at",
            "updated_at",
            "reviewed_by",
            "role",
            "role_color",
            "reviewer_name",
            "reviewed_at",
            "rejection_reason",
            "notes",
        ]
        read_only_fields = fields

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()

    def get_reviewer_name(self, obj):
        if obj.reviewed_by:
            return f"{obj.reviewed_by.first_name} {obj.reviewed_by.last_name}".strip()
        return None

    def get_user_avatar(self, obj):
        if (
            not obj.user
            or not obj.user.avatar
            or not hasattr(obj.user.avatar, "name")
            or not obj.user.avatar.name
        ):
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None
        original_path = obj.user.avatar.name
        if not original_path.startswith("originals/"):
            return None
        base_path, _ = os.path.splitext(original_path)
        resized_base_path = base_path.replace(
            "originals/", "public/medium/", 1
        )  # Using medium for detail
        webp_path = resized_base_path + ".webp"
        return f"{settings.CLOUDFRONT_DOMAIN}/{webp_path}"

    def get_business_image_medium_url(self, obj):
        if (
            not obj.business
            or not obj.business.businessImage
            or not hasattr(obj.business.businessImage, "name")
            or not obj.business.businessImage.name
        ):
            return None
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None
        original_path = obj.business.businessImage.name
        if not original_path.startswith("originals/"):
            return None
        base_path, _ = os.path.splitext(original_path)
        resized_base_path = base_path.replace("originals/", "public/medium/", 1)
        webp_path = resized_base_path + ".webp"
        return f"{settings.CLOUDFRONT_DOMAIN}/{webp_path}"


class VerificationSubmissionSerializer(serializers.ModelSerializer):
    """Serializer for users submitting verification requests"""

    documents = serializers.ListField(child=serializers.FileField(), write_only=True)
    document_types = serializers.ListField(
        child=serializers.ChoiceField(choices=VerificationDocument.DOCUMENT_TYPES),
        write_only=True,
    )

    class Meta:
        model = VerificationRequest
        fields = ["business", "documents", "document_types", "notes"]

    def validate(self, data):
        if len(data["documents"]) != len(data["document_types"]):
            raise serializers.ValidationError(
                "Number of documents and document types must match."
            )
        return data

    def create(self, validated_data):
        documents = validated_data.pop("documents")
        document_types = validated_data.pop("document_types")

        request = VerificationRequest.objects.create(
            user=self.context["request"].user, **validated_data
        )

        for i, document_file in enumerate(documents):
            VerificationDocument.objects.create(
                verification_request=request,
                document_type=document_types[i],
                file=document_file,
                filename=document_file.name,
                file_type=document_file.content_type,
            )
        return request


class VerificationProcessSerializer(serializers.ModelSerializer):
    """Serializer for admins to process verification requests"""

    status = serializers.ChoiceField(
        choices=[("approved", "Approved"), ("rejected", "Rejected")]
    )

    class Meta:
        model = VerificationRequest
        fields = ["status", "rejection_reason", "notes"]

    def validate(self, data):
        if data.get("status") == "rejected" and not data.get("rejection_reason"):
            raise serializers.ValidationError(
                {
                    "rejection_reason": "Rejection reason is required when rejecting a request."
                }
            )
        if data.get("status") == "approved":
            data["rejection_reason"] = ""
        return data

    def update(self, instance, validated_data):
        backend_status = (
            "verified" if validated_data["status"] == "approved" else "rejected"
        )
        instance.reviewed_by = self.context["request"].user
        instance.reviewed_at = timezone.now()
        instance.status = backend_status
        instance.rejection_reason = validated_data.get("rejection_reason", "")
        instance.notes = validated_data.get("notes", instance.notes)
        instance.save()

        if instance.business:
            instance.business.verificationStatus = instance.status
            if instance.status == "verified":
                instance.business.isActive = True
            elif instance.status == "rejected":
                instance.business.isActive = False
            instance.business.save(update_fields=["verificationStatus", "isActive"])

        return instance
