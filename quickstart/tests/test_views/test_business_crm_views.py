import io
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.db.models import Q

from quickstart.models import BusinessInfo
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    RoleFactory,
)
from quickstart.utils.permissions import IsBusinessOwnerOrManager


class ContactImportTests(APITestCase):
    """
    Tests for the ContactImportViewSet, covering file upload and processing initiation.
    """

    def setUp(self):
        self.user = UserFactory()
        business_role = RoleFactory(name="Business Owner")
        # Add a placeholder permission, though the check is more direct in the view
        # self.user.user_permissions.add(...)
        self.user.role = business_role
        self.user.save()
        self.business = BusinessInfoFactory(owner=self.user)
        self.client.force_authenticate(user=self.user)
        self.upload_url = reverse("business-contact-import-upload-file")
        self.process_url = reverse("business-contact-import-start-processing")

    def test_upload_csv_file_succeeds_and_returns_headers(self):
        """
        POST /api/business/contact-import/upload/ - Test uploading a CSV.
        """
        print("\n--- Running: test_upload_csv_file_succeeds_and_returns_headers ---")
        csv_content = (
            b"First Name,Last Name,Email Address\nJohn,Doe,john.doe@example.com"
        )
        csv_file = SimpleUploadedFile(
            "contacts.csv", csv_content, content_type="text/csv"
        )

        response = self.client.post(
            self.upload_url, {"file": csv_file}, format="multipart"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("file_path", response.data)
        self.assertTrue(response.data["file_path"].startswith("imports/"))
        self.assertEqual(
            response.data["headers"], ["First Name", "Last Name", "Email Address"]
        )
        print("✅ PASSED: CSV upload and header parsing successful.")

    def test_upload_xlsx_file_succeeds_and_returns_headers(self):
        """
        POST /api/business/contact-import/upload/ - Test uploading an XLSX file.
        """
        # This requires `openpyxl`. We create a dummy xlsx file in memory.
        import pandas as pd
        from io import BytesIO

        df = pd.DataFrame(
            {"Full Name": ["Jane Doe"], "Contact Email": ["jane.doe@example.com"]}
        )
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Sheet1")
        output.seek(0)
        xlsx_file = SimpleUploadedFile(
            "contacts.xlsx",
            output.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post(
            self.upload_url, {"file": xlsx_file}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("file_path", response.data)
        self.assertEqual(response.data["headers"], ["Full Name", "Contact Email"])
        print("✅ PASSED: XLSX upload and header parsing successful.")

    @patch("quickstart.tasks.crm_tasks.process_contact_import.delay")
    def test_start_processing_launches_background_task(self, mock_process_task):
        """
        POST /api/business/contact-import/start-processing/ - Test task launch.
        """
        print("\n--- Running: test_start_processing_launches_background_task ---")
        mock_process_task.return_value.id = "test-task-id-123"
        file_path = "imports/dummy-path.csv"
        column_mapping = {
            "First Name": "first_name",
            "Last Name": "last_name",
            "Email Address": "email",
        }

        data = {"file_path": file_path, "column_mapping": column_mapping}

        response = self.client.post(self.process_url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["task_id"], "test-task-id-123")
        mock_process_task.assert_called_once_with(
            file_path, column_mapping, self.business.businessId
        )
        print("✅ PASSED: Contact import processing task was successfully launched.")
