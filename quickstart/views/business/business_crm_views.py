from rest_framework import viewsets, status
from rest_framework.decorators import (
    action,
    parser_classes,
)  # Import parser_classes decorator
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import (
    MultiPartParser,
    FormParser,
    JSONParser,
)  # Import JSONParser
from django.core.files.storage import default_storage
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

import pandas as pd
import os
import uuid

from quickstart.models import BusinessInfo
from quickstart.utils.permissions import (
    IsBusinessOwnerOrManager,
)
from quickstart.tasks.crm_tasks import process_contact_import
from quickstart.serializers import (
    ContactImportUploadSerializer,
)

import logging

logger = logging.getLogger(__name__)


class ContactImportViewSet(viewsets.ViewSet):
    """
    Manages the multi-step process for importing contacts from a file.
    """

    permission_classes = [IsAuthenticated, IsBusinessOwnerOrManager]
    # REMOVED: parser_classes are now defined per-action

    def get_business_context(self):
        # Helper to get the user's business
        user = self.request.user
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            raise PermissionDenied("You are not associated with a business.")
        return business

    @action(detail=False, methods=["post"], url_path="upload")
    @parser_classes(
        [MultiPartParser, FormParser]
    )  # ADDED: Apply parsers only to this action
    def upload_file(self, request, *args, **kwargs):
        """
        Step 1: Upload the file, parse its headers from memory, then save to storage.
        """
        serializer = ContactImportUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        file = serializer.validated_data["file"]

        try:
            # Read headers directly from the in-memory uploaded file
            df = (
                pd.read_excel(file, nrows=0, engine="openpyxl")
                if file.name.endswith((".xlsx", ".xls"))
                else pd.read_csv(file, nrows=0)
            )
            headers = df.columns.tolist()

            # Rewind the file pointer to the beginning before saving
            file.seek(0)

            # Save the complete file to default storage (S3, etc.)
            file_extension = os.path.splitext(file.name)[1]
            temp_filename = f"imports/{uuid.uuid4()}{file_extension}"
            relative_file_path = default_storage.save(temp_filename, file)

            return Response(
                {
                    "file_path": relative_file_path,
                    "headers": headers,
                    "message": "File uploaded successfully. Please map the columns.",
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.error(
                f"Failed to read headers from uploaded file stream: {e}", exc_info=True
            )
            return Response(
                {"error": f"Could not read file headers: {e}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=False, methods=["post"], url_path="start-processing")
    # This action will now correctly use the default JSONParser
    def start_processing(self, request, *args, **kwargs):
        """
        Step 2: User provides column mapping, start the background task.
        """
        file_path = request.data.get("file_path")
        column_mapping = request.data.get("column_mapping")

        if not file_path or not column_mapping:
            return Response(
                {"error": "file_path and column_mapping are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Basic validation for mapping
        required_fields = ["first_name", "email"]
        if not any(field in column_mapping.values() for field in required_fields):
            return Response(
                {"error": "You must map at least a 'First Name' or 'Email' column."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        business = self.get_business_context()

        # Launch the background task
        task = process_contact_import.delay(
            file_path, column_mapping, business.businessId
        )

        return Response(
            {
                "task_id": task.id,
                "message": "Import process has started. We will notify you upon completion.",
            },
            status=status.HTTP_202_ACCEPTED,
        )
