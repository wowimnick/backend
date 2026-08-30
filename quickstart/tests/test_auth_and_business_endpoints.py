"""
Tests for auth endpoints and business-related endpoints (views from auth + business).
Covers: login, logout, token refresh, registration, password reset, user profile/update,
my-favorites; business register, my-businesses, my-business/overview, my-business/profile,
business-stats, business/all-categories; and business router ViewSets (classes, staff, etc.).
Run with: pytest quickstart/tests/test_auth_and_business_endpoints.py -v
"""
import pytest
from rest_framework import status

from quickstart.tests.factories import (
    UserFactory,
    BusinessFactory,
    ClassCategoryFactory,
)

API = "/api"


# -----------------------------------------------------------------------------
# Fixtures (business_owner_client lives in conftest.py)
# -----------------------------------------------------------------------------


def valid_business_registration_payload():
    """Minimal valid payload for POST /api/business/register/."""
    return {
        "businessName": "New Test Business",
        "website": "https://example.com",
        "studentContactPhone": "+15551234567",
        "termsAccepted": True,
        "privacyAccepted": True,
        "plan_id": "growth",
    }


# -----------------------------------------------------------------------------
# Auth: Login
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestLogin:
    def test_login_missing_credentials_401(self, api_client):
        """POST /api/login/ with no body returns 401 or 400 (validation error)."""
        response = api_client.post(f"{API}/login/", {}, format="json")
        assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_401_UNAUTHORIZED)

    def test_login_invalid_credentials_401(self, api_client):
        """POST /api/login/ with wrong password returns 401."""
        user = UserFactory()
        response = api_client.post(
            f"{API}/login/",
            {"email": user.email, "password": "wrongpassword"},
            format="json",
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_login_valid_credentials_200(self, api_client):
        """POST /api/login/ with valid email/password returns 200 and user in body."""
        password = "TestPass123!"
        user = UserFactory()
        user.set_password(password)
        user.save()
        response = api_client.post(
            f"{API}/login/",
            {"email": user.email, "password": password},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "user" in data
        assert data["user"]["email"] == user.email


# -----------------------------------------------------------------------------
# Auth: Logout
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestLogout:
    def test_logout_post_205(self, api_client):
        """POST /api/logout/ returns 205 (clears cookies)."""
        response = api_client.post(f"{API}/logout/")
        assert response.status_code == status.HTTP_205_RESET_CONTENT


# -----------------------------------------------------------------------------
# Auth: Token refresh
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestTokenRefresh:
    def test_token_refresh_no_cookie_401(self, api_client):
        """POST /api/token/refresh/ without refresh cookie returns 401."""
        response = api_client.post(f"{API}/token/refresh/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# -----------------------------------------------------------------------------
# Auth: Registration
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegistration:
    def test_register_missing_fields_400(self, api_client):
        """POST /api/auth/registration/ with missing required fields returns 400."""
        response = api_client.post(
            f"{API}/auth/registration/",
            {"email": "new@example.com"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_register_duplicate_email_400(self, api_client, user):
        """POST /api/auth/registration/ with existing email returns 400."""
        payload = {
            "email": user.email,
            "password1": "Str0ng!Pass",
            "password2": "Str0ng!Pass",
            "first_name": "First",
            "last_name": "Last",
            "phone_number": "+15551234567",
        }
        response = api_client.post(
            f"{API}/auth/registration/",
            payload,
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_register_valid_201(self, api_client):
        """POST /api/auth/registration/ with valid payload returns 201; may include user or detail (verification email)."""
        payload = {
            "email": "newuser@example.com",
            "password1": "Str0ng!Pass",
            "password2": "Str0ng!Pass",
            "first_name": "New",
            "last_name": "User",
            "phone_number": "+15551234567",
        }
        response = api_client.post(
            f"{API}/auth/registration/",
            payload,
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert "user" in data or "detail" in data
        if "user" in data:
            assert data["user"]["email"] == "newuser@example.com"


# -----------------------------------------------------------------------------
# Auth: Password reset
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestPasswordReset:
    def test_password_reset_missing_email_400(self, api_client):
        """POST /api/auth/password/reset/ without email returns 400."""
        response = api_client.post(
            f"{API}/auth/password/reset/",
            {},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_password_reset_valid_email_200(self, api_client, user):
        """POST /api/auth/password/reset/ with valid email returns 200 (always, for privacy)."""
        response = api_client.post(
            f"{API}/auth/password/reset/",
            {"email": user.email},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestPasswordResetConfirm:
    def test_password_reset_confirm_invalid_uid_400(self, api_client):
        """POST /api/auth/password/reset/confirm/ with invalid uid returns 400."""
        response = api_client.post(
            f"{API}/auth/password/reset/confirm/",
            {"uid": 999999, "token": "invalid", "new_password1": "NewPass123!", "new_password2": "NewPass123!"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# -----------------------------------------------------------------------------
# Auth: User profile and update
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestUserProfile:
    def test_user_profile_unauthenticated_401(self, api_client):
        """GET /api/user/profile/ without auth returns 401."""
        response = api_client.get(f"{API}/user/profile/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_user_profile_authenticated_200(self, authenticated_client, user):
        """GET /api/user/profile/ when authenticated returns 200 and user data."""
        response = authenticated_client.get(f"{API}/user/profile/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["email"] == user.email

    def test_user_update_unauthenticated_401(self, api_client):
        """PATCH /api/user/update/ without auth returns 401."""
        response = api_client.patch(
            f"{API}/user/update/",
            {"first_name": "Updated"},
            format="json",
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_user_update_authenticated_200(self, authenticated_client, user):
        """PATCH /api/user/update/ when authenticated returns 200 and updated data."""
        response = authenticated_client.patch(
            f"{API}/user/update/",
            {"first_name": "UpdatedName"},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json().get("first_name") == "UpdatedName"


# -----------------------------------------------------------------------------
# Auth: My favorites
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestMyFavorites:
    def test_my_favorites_unauthenticated_401(self, api_client):
        """GET /api/my-favorites/ without auth returns 401."""
        response = api_client.get(f"{API}/my-favorites/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_my_favorites_authenticated_200(self, authenticated_client):
        """GET /api/my-favorites/ when authenticated returns 200 (may be empty)."""
        response = authenticated_client.get(f"{API}/my-favorites/")
        assert response.status_code == status.HTTP_200_OK


# -----------------------------------------------------------------------------
# Business: Register
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestBusinessRegister:
    def test_register_business_unauthenticated_401(self, api_client):
        """POST /api/business/register/ without auth returns 401."""
        response = api_client.post(
            f"{API}/business/register/",
            valid_business_registration_payload(),
            format="json",
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_register_business_already_has_business_409(self, api_client, business):
        """POST /api/business/register/ when user already owns a business returns 409."""
        payload = valid_business_registration_payload()
        api_client.force_authenticate(user=business.owner)
        response = api_client.post(
            f"{API}/business/register/",
            payload,
            format="json",
        )
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_register_business_valid_201(self, api_client, user):
        """POST /api/business/register/ with valid payload returns 201 and business id."""
        api_client.force_authenticate(user=user)
        payload = valid_business_registration_payload()
        response = api_client.post(
            f"{API}/business/register/",
            payload,
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data.get("success") is True
        assert "businessId" in data
        assert "user" in data


# -----------------------------------------------------------------------------
# Business: My businesses
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestMyBusinesses:
    def test_my_businesses_unauthenticated_401(self, api_client):
        """GET /api/my-businesses/ without auth returns 401."""
        response = api_client.get(f"{API}/my-businesses/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_my_businesses_authenticated_200(self, business_owner_client, business):
        """GET /api/my-businesses/ when authenticated returns 200 and list including owned business."""
        response = business_owner_client.get(f"{API}/my-businesses/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)
        slugs = [b.get("slug") for b in data]
        assert business.slug in slugs

    def test_my_businesses_user_without_business_200_empty(self, authenticated_client):
        """GET /api/my-businesses/ for user with no business returns 200 and empty list."""
        response = authenticated_client.get(f"{API}/my-businesses/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []


# -----------------------------------------------------------------------------
# Business: My business overview
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestMyBusinessOverview:
    def test_my_business_overview_unauthenticated_401(self, api_client):
        """GET /api/my-business/overview/ without auth returns 401."""
        response = api_client.get(f"{API}/my-business/overview/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_my_business_overview_user_without_business_403(self, authenticated_client):
        """GET /api/my-business/overview/ for user with no business returns 403."""
        response = authenticated_client.get(f"{API}/my-business/overview/")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_my_business_overview_owner_200(self, business_owner_client):
        """GET /api/my-business/overview/ for business owner returns 200 with dashboard payload."""
        response = business_owner_client.get(f"{API}/my-business/overview/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "metrics" in data and "today_snapshot" in data


# -----------------------------------------------------------------------------
# Business: My business profile
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestMyBusinessProfile:
    def test_my_business_profile_unauthenticated_401(self, api_client):
        """GET /api/my-business/profile/ without auth returns 401."""
        response = api_client.get(f"{API}/my-business/profile/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_my_business_profile_owner_200(self, business_owner_client, business):
        """GET /api/my-business/profile/ for business owner returns 200 and business data."""
        response = business_owner_client.get(f"{API}/my-business/profile/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data.get("slug") == business.slug or data.get("businessName") == business.businessName

    def test_my_business_profile_patch_onboarding_survey(self, business_owner_client, business):
        """PATCH /api/my-business/profile/ stores optional onboarding survey answers."""
        response = business_owner_client.patch(
            f"{API}/my-business/profile/",
            {
                "onboarding_survey": {
                    "industry": "arts",
                    "booking_system": "calendly",
                    "attribution": "google",
                    "estimated_monthly_volume": 2500,
                }
            },
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK
        business.refresh_from_db()
        assert business.onboarding_survey["industry"] == "arts"
        assert business.onboarding_survey["booking_system"] == "calendly"
        assert business.onboarding_survey["attribution"] == "google"
        assert business.onboarding_survey["estimated_monthly_volume"] == 2500

    def test_my_business_profile_patch_onboarding_survey_rejects_unknown(self, business_owner_client):
        response = business_owner_client.patch(
            f"{API}/my-business/profile/",
            {"onboarding_survey": {"industry": "not-a-vertical"}},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


# -----------------------------------------------------------------------------
# Business: Business stats (dashboard)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestBusinessStats:
    def test_business_stats_list_unauthenticated_401(self, api_client):
        """GET /api/business-stats/ without auth returns 401."""
        response = api_client.get(f"{API}/business-stats/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_business_stats_list_owner_200(self, business_owner_client, business):
        """GET /api/business-stats/ for business owner returns 200 and list."""
        response = business_owner_client.get(f"{API}/business-stats/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        if isinstance(results, list):
            ids = [r.get("businessId") or r.get("business_id") or r.get("pk") for r in results]
            assert business.businessId in ids or business.pk in ids


# -----------------------------------------------------------------------------
# Business: All categories (public-style for business forms)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestBusinessAllCategories:
    def test_all_categories_returns_200(self, api_client):
        """GET /api/business/all-categories/ returns 200 (AllowAny)."""
        response = api_client.get(f"{API}/business/all-categories/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)

    def test_all_categories_with_existing_returns_list(self, api_client):
        """GET /api/business/all-categories/ returns a list (may be empty due to caching)."""
        ClassCategoryFactory()
        response = api_client.get(f"{API}/business/all-categories/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert isinstance(data, list)


# -----------------------------------------------------------------------------
# Business router ViewSets (views/business/* under /api/business/)
# -----------------------------------------------------------------------------

BUSINESS_ROUTER = f"{API}/business"


@pytest.mark.django_db
class TestBusinessRouterClasses:
    """BusinessClassViewSet: /api/business/classes/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/classes/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/classes/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "results" in data or isinstance(data, list)


@pytest.mark.django_db
class TestBusinessRouterStaff:
    """BusinessStaffViewSet: /api/business/staff/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/staff/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/staff/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterRoles:
    """BusinessRoleViewSet: /api/business/roles/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/roles/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/roles/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterBookings:
    """BusinessBookingViewSet: /api/business/bookings/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/bookings/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/bookings/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterStudents:
    """BusinessStudentViewSet: /api/business/students/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/students/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/students/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterSchedules:
    """BusinessScheduleViewSet: /api/business/schedules/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/schedules/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/schedules/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterPayouts:
    """BusinessPayoutViewSet: /api/business/payouts/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/payouts/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/payouts/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterReviews:
    """BusinessReviewViewSet: /api/business/reviews/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/reviews/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/reviews/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterNotifications:
    """NotificationViewSet: /api/business/notifications/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/notifications/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/notifications/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterDiscounts:
    """BusinessDiscountViewSet: /api/business/discounts/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/discounts/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/discounts/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterCourseManagement:
    """BusinessCourseManagementViewSet: /api/business/course-management/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/course-management/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/course-management/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterScheduleInstances:
    """BusinessScheduleInstanceViewSet: /api/business/schedule-instances/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/schedule-instances/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client):
        response = business_owner_client.get(f"{BUSINESS_ROUTER}/schedule-instances/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterStudentCourseEnrollments:
    """StudentCourseEnrollmentViewSet: /api/business/student/course-enrollments/"""

    def test_list_unauthenticated_401(self, api_client):
        response = api_client.get(f"{BUSINESS_ROUTER}/student/course-enrollments/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_authenticated_200(self, authenticated_client):
        response = authenticated_client.get(f"{BUSINESS_ROUTER}/student/course-enrollments/")
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessRouterContactImport:
    """ContactImportViewSet: /api/business/contact-import/ (custom actions only, no list)."""

    def test_upload_unauthenticated_401(self, api_client):
        response = api_client.post(f"{BUSINESS_ROUTER}/contact-import/upload/", {})
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_upload_owner_no_file_400(self, business_owner_client):
        response = business_owner_client.post(
            f"{BUSINESS_ROUTER}/contact-import/upload/",
            {},
            format="multipart",
        )
        assert response.status_code in (status.HTTP_400_BAD_REQUEST, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
