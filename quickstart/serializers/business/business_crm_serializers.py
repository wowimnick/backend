import os
from rest_framework import serializers

# A reasonable file size limit: 10 MB
MAX_FILE_SIZE = 10 * 1024 * 1024
# Whitelist of allowed content types for security
ALLOWED_CONTENT_TYPES = [
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]


class ContactImportUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        # 1. File size validation
        if value.size > MAX_FILE_SIZE:
            raise serializers.ValidationError(
                f"File size cannot exceed {MAX_FILE_SIZE // 1024 // 1024}MB."
            )

        # 2. File type validation (based on MIME type, not just extension)
        if value.content_type not in ALLOWED_CONTENT_TYPES:
            raise serializers.ValidationError(
                f"Unsupported file type '{value.content_type}'. Please use CSV or Excel."
            )

        # 3. Basic extension check as a fallback
        valid_extensions = [".csv", ".xlsx", ".xls"]
        ext = os.path.splitext(value.name)[1]
        if ext.lower() not in valid_extensions:
            raise serializers.ValidationError(
                "Invalid file extension. Please use CSV or Excel."
            )

        return value
