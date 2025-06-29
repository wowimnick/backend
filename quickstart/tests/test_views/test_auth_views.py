from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch
from PIL import Image
import io
from django.conf import settings

from quickstart.models import CustomUser, Role
from quickstart.tests.factories import UserFactory, RoleFactory


class AuthAndProfileTests(APITestCase):
    """
    Tests for user registration, login, logout, and profile management.
    """

    def setUp(self):
        RoleFactory(name="Student", is_default=True)
        self.password = "strongpassword123"
        self.user = UserFactory(
            email="test@example.com",
        )
        self.user.set_password(self.password)
        self.user.save()

    def test_successful_registration(self):
        """
        POST /api/auth/registration/ - Ensure a new user can register successfully.
        """
        print("\n--- Running: test_successful_registration ---")
        url = reverse("rest_register")
        data = {
            "email": "newuser@example.com",
            "username": "newuser",
            "password1": "newpassword123",
            "password2": "newpassword123",
            "first_name": "New",
            "last_name": "User",
        }

        # FIX: The correct path to mock is in allauth, which dj-rest-auth uses internally.
        with patch("allauth.account.utils.send_email_confirmation") as mock_send_email:
            response = self.client.post(url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(CustomUser.objects.filter(email="newuser@example.com").exists())
        self.assertTrue(mock_send_email.called, "Email confirmation should be sent.")
        print("✅ PASSED: Successful user registration.")

    def test_duplicate_email_registration_fails(self):
        """
        POST /api/auth/registration/ - Ensure registration fails for a duplicate email.
        """
        print("\n--- Running: test_duplicate_email_registration_fails ---")
        url = reverse("rest_register")
        data = {
            "email": "test@example.com",
            "username": "anotheruser",
            "password1": "a-much-stronger-password-123",
            "password2": "a-much-stronger-password-123",
            "first_name": "Another",
            "last_name": "User",
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)
        self.assertEqual(response.data["email"][0].code, "unique")
        print("✅ PASSED: Duplicate email registration prevented.")

    def test_successful_login(self):
        """
        POST /api/login/ - Ensure a user can log in and receive tokens in cookies.
        """
        print("\n--- Running: test_successful_login ---")
        url = reverse("token_obtain_pair")
        data = {"email": "test@example.com", "password": self.password}
        response = self.client.post(url, data)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("user", response.data)
        self.assertEqual(response.data["user"]["email"], self.user.email)
        # FIX: Use the configured cookie names from settings.
        self.assertIn(settings.SIMPLE_JWT["AUTH_COOKIE"], response.cookies)
        self.assertIn(settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"], response.cookies)
        print("✅ PASSED: Successful login with token cookies.")

    def test_login_with_wrong_password_fails(self):
        """
        POST /api/login/ - Ensure login fails with an incorrect password.
        """
        print("\n--- Running: test_login_with_wrong_password_fails ---")
        url = reverse("token_obtain_pair")
        data = {"email": "test@example.com", "password": "wrongpassword"}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        print("✅ PASSED: Login with wrong password failed as expected.")

    def test_successful_logout(self):
        """
        POST /api/logout/ - Ensure logout clears token cookies.
        """
        print("\n--- Running: test_successful_logout ---")
        login_url = reverse("token_obtain_pair")
        login_data = {"email": "test@example.com", "password": self.password}
        self.client.post(login_url, login_data)

        logout_url = reverse("logout")
        response = self.client.post(logout_url)

        self.assertEqual(response.status_code, status.HTTP_205_RESET_CONTENT)
        # FIX: Use the configured cookie names from settings and check that max_age is set to 0 on deletion.
        self.assertEqual(
            response.cookies[settings.SIMPLE_JWT["AUTH_COOKIE"]]["max-age"], 0
        )
        self.assertEqual(
            response.cookies[settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"]]["max-age"], 0
        )
        print("✅ PASSED: Successful logout and cookie clearing.")

    def test_retrieve_own_profile(self):
        """
        GET /api/user/profile/ - An authenticated user can retrieve their own profile.
        """
        print("\n--- Running: test_retrieve_own_profile ---")
        self.client.force_authenticate(user=self.user)
        url = reverse("my-profile")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], self.user.email)
        print("✅ PASSED: User can retrieve their own profile.")

    def test_update_own_profile(self):
        """
        PATCH /api/user/update/ - An authenticated user can update their profile.
        """
        print("\n--- Running: test_update_own_profile ---")
        self.client.force_authenticate(user=self.user)
        url = reverse("user-update")

        # FIX: Create a real, valid image in memory using Pillow.
        image = Image.new("RGB", (100, 100))
        image_io = io.BytesIO()
        image.save(image_io, "JPEG")
        image_io.seek(0)
        avatar = SimpleUploadedFile(
            "avatar.jpg", image_io.read(), content_type="image/jpeg"
        )

        data = {
            "first_name": "Updated First",
            "bio": "This is my new bio.",
            "avatar": avatar,
        }

        response = self.client.patch(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Updated First")
        self.assertTrue(self.user.avatar.name.startswith("avatars/avatar"))
        print("✅ PASSED: User can update their profile information.")

    def test_cannot_update_readonly_fields(self):
        """
        PATCH /api/user/update/ - A user cannot update read-only fields like email.
        """
        print("\n--- Running: test_cannot_update_readonly_fields ---")
        self.client.force_authenticate(user=self.user)
        url = reverse("user-update")

        original_email = self.user.email
        data = {"email": "cannotchange@example.com"}

        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, original_email)
        print("✅ PASSED: Read-only profile fields were not updated.")
