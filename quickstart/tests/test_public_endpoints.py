"""
Unit tests for public API endpoints.
All tests use Factory Boy for data; pytest-django rolls back the DB after each test.

URLs: The test client does NOT call http://localhost:8000. It calls Django's URL routing
with the path only. So path "/api/health-check/" is the same as
http://localhost:8000/api/health-check/ (one /api/ prefix, defined here only).

Run with: pytest quickstart/tests/test_public_endpoints.py -v
To see print/debug output if tests hang: add -s (e.g. pytest ... -v -s)
"""
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import pytz
from django.urls import reverse
from django.utils import timezone as django_timezone

from quickstart.models import Booking, GiftCard

# Single prefix for API paths (no double /api/api/). Equals http://localhost:8000/api/...
API = "/api"
from quickstart.tests.factories import (
    UserFactory,
    BusinessFactory,
    ContactFactory,
    ClassCategoryFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
    BookingFactory,
    GiftCardFactory,
    BlogCategoryFactory,
    BlogPostFactory,
)


# -----------------------------------------------------------------------------
# Health & CSRF
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestHealthCheck:
    def test_health_check_get_returns_ok(self, api_client):
        """GET /api/health-check/ returns 200 OK for load balancers."""
        print("[test] test_health_check_get_returns_ok started (DB setup done)")
        path = f"{API}/health-check/"
        print(f"[test] Requesting path: {path} (same as http://localhost:8000{path})")
        print("[test] Calling api_client.get()...")
        response = api_client.get(path)
        print(f"[test] Got response: status={response.status_code}")
        assert response.status_code == 200
        assert response.content.decode() == "OK"


@pytest.mark.django_db
class TestCSRF:
    def test_csrf_cookie_endpoint_returns_200(self, api_client):
        """GET /api/csrf/ returns 200 (cookie set by middleware in real requests)."""
        response = api_client.get(f"{API}/csrf/")
        assert response.status_code == 200


# -----------------------------------------------------------------------------
# Public businesses (list & retrieve)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicBusinesses:
    def test_business_list_returns_only_active_verified(self, api_client, business):
        """GET /api/businesses/ returns only active, verified businesses."""
        inactive = BusinessFactory(
            slug="inactive-biz",
            isActive=False,
            verificationStatus="verified",
        )
        unverified = BusinessFactory(
            slug="unverified-biz",
            isActive=True,
            verificationStatus="pending",
        )
        response = api_client.get(f"{API}/businesses/")
        assert response.status_code == 200
        results = response.json().get("results", response.json())
        slugs = [b["slug"] for b in results] if isinstance(results, list) else []
        assert business.slug in slugs
        assert inactive.slug not in slugs
        assert unverified.slug not in slugs

    def test_business_retrieve_by_slug(self, api_client, business):
        """GET /api/businesses/<slug>/ returns business detail."""
        response = api_client.get(f"{API}/businesses/{business.slug}/")
        assert response.status_code == 200
        data = response.json()
        assert data["slug"] == business.slug
        assert data["businessName"] == business.businessName

    def test_business_retrieve_404_for_invalid_slug(self, api_client):
        """GET /api/businesses/<bad>/ returns 404."""
        response = api_client.get(f"{API}/businesses/nonexistent-slug/")
        assert response.status_code == 404


# -----------------------------------------------------------------------------
# Public classes (list, retrieve, search, homepage)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicClasses:
    def test_class_list_returns_active_classes(self, api_client, public_class):
        """GET /api/classes/ returns list of classes."""
        response = api_client.get(f"{API}/classes/")
        assert response.status_code == 200
        results = response.json().get("results", response.json())
        ids = [c.get("classId") or c.get("slug") for c in results]
        assert public_class.classId in ids or public_class.slug in ids

    def test_class_retrieve_by_slug(self, api_client, public_class):
        """GET /api/classes/<slug>/ returns class detail."""
        response = api_client.get(f"{API}/classes/{public_class.slug}/")
        assert response.status_code == 200
        data = response.json()
        assert data.get("slug") == public_class.slug or data.get("title") == public_class.title

    def test_class_search_accepts_query(self, api_client, public_class):
        """GET /api/classes/search/?q=... returns search results."""
        response = api_client.get(f"{API}/classes/search/", {"q": "Test"})
        assert response.status_code == 200

    def test_class_search_count_only_returns_minimal_json(self, api_client, public_class):
        """GET /api/classes/search/?count_only=1 returns only {count} (no result row serialization)."""
        response = api_client.get(f"{API}/classes/search/", {"count_only": "1"})
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == {"count"}
        assert isinstance(data["count"], int)

    def test_class_homepage_content_returns_200(self, api_client):
        """GET /api/classes/homepage-content/ returns 200."""
        response = api_client.get(f"{API}/classes/homepage-content/")
        assert response.status_code == 200


# -----------------------------------------------------------------------------
# Public blog
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicBlog:
    def test_blog_post_list_returns_published(self, api_client):
        """GET /api/blog/posts/ returns published posts."""
        post = BlogPostFactory(status="published")
        draft = BlogPostFactory(status="draft", slug="draft-post")
        response = api_client.get(f"{API}/blog/posts/")
        assert response.status_code == 200
        results = response.json().get("results", response.json())
        slugs = [p.get("slug") for p in results]
        assert post.slug in slugs
        assert draft.slug not in slugs

    def test_blog_post_retrieve_by_slug(self, api_client):
        """GET /api/blog/posts/<slug>/ returns post detail."""
        post = BlogPostFactory(status="published", slug="my-blog-post")
        response = api_client.get(f"{API}/blog/posts/my-blog-post/")
        assert response.status_code == 200
        assert response.json().get("slug") == "my-blog-post"

    def test_blog_category_list_returns_200(self, api_client):
        """GET /api/blog/categories/ returns categories."""
        BlogCategoryFactory()
        response = api_client.get(f"{API}/blog/categories/")
        assert response.status_code == 200


# -----------------------------------------------------------------------------
# Public categories & courses
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicCategoriesAndCourses:
    def test_categories_list_returns_200(self, api_client):
        """GET /api/categories/ returns categories."""
        ClassCategoryFactory()
        response = api_client.get(f"{API}/categories/")
        assert response.status_code == 200

    def test_courses_list_returns_200(self, api_client, business):
        """GET /api/courses/ returns courses (public list)."""
        response = api_client.get(f"{API}/courses/")
        assert response.status_code == 200

    def test_schedules_list_returns_200(self, api_client):
        """GET /api/schedules/ returns schedules (public list)."""
        response = api_client.get(f"{API}/schedules/")
        assert response.status_code == 200


# -----------------------------------------------------------------------------
# Public redirects (class ID → slug, business ID → slug)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicRedirects:
    def test_class_id_redirect_to_slug(self, api_client, public_class):
        """GET /api/classes/<int:class_id>/ redirects to /classes/<slug>/."""
        response = api_client.get(f"{API}/classes/{public_class.classId}/")
        assert response.status_code in (301, 302)
        assert public_class.slug in (response.get("Location") or "")

    def test_business_id_redirect_to_slug(self, api_client, business):
        """GET /api/business/<int:business_id>/ redirects to /business/<slug>/."""
        response = api_client.get(f"{API}/business/{business.businessId}/")
        assert response.status_code in (301, 302)
        assert business.slug in (response.get("Location") or "")


# -----------------------------------------------------------------------------
# Public business: Google reviews, staff invitation (validate / accept)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestPublicBusinessEndpoints:
    def test_google_reviews_list_returns_200(self, api_client, business):
        """GET /api/business/<id>/google-reviews/ returns 200 (may be empty)."""
        response = api_client.get(
            f"{API}/business/{business.businessId}/google-reviews/"
        )
        assert response.status_code == 200

    def test_validate_invitation_missing_token_400(self, api_client):
        """GET /api/business/validate-invitation/?token= missing/invalid returns 400 or 404."""
        response = api_client.get(f"{API}/business/validate-invitation/")
        assert response.status_code in (400, 404)

    def test_validate_invitation_invalid_token_404(self, api_client):
        """GET /api/business/validate-invitation/?token=<bad> returns 404."""
        response = api_client.get(
            f"{API}/business/validate-invitation/",
            {"token": str(uuid.uuid4())},
        )
        assert response.status_code in (400, 404)

    def test_accept_invitation_invalid_payload_400(self, api_client):
        """POST /api/business/accept-invitation/ with invalid token returns 400/401/404."""
        response = api_client.post(
            f"{API}/business/accept-invitation/",
            {"token": "invalid"},
            format="json",
        )
        assert response.status_code in (400, 401, 404)


# -----------------------------------------------------------------------------
# Guest booking cancellation (public, token-based)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestGuestBookingCancellation:
    def test_guest_cancel_get_returns_booking_info(self, api_client):
        """GET guest-cancel/<token> returns booking details when valid and confirmed."""
        business = BusinessFactory(isActive=True, verificationStatus="verified")
        klass = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=klass, cancellationPolicy="flexible")
        schedule = ScheduleFactory(option=option)
        instance = ScheduleInstanceFactory(
            schedule=schedule,
            date=date.today() + timedelta(days=14),
            time=time(10, 0),
            status="scheduled",
        )
        contact = ContactFactory(business=business)
        token = uuid.uuid4()
        booking = Booking.objects.create(
            schedule_instance=instance,
            contact=contact,
            user=None,
            participants=1,
            amount_paid=Decimal("25.00"),
            payment_status="paid",
            status="confirmed",
            enrollment_type="Single Session",
            cancellation_policy="flexible",
            cancellation_token=token,
        )
        response = api_client.get(f"{API}/bookings/guest-cancel/{token}/")
        assert response.status_code == 200
        data = response.json()
        assert data.get("id") == booking.id or "schedule_instance" in str(data).lower() or "status" in data

    def test_guest_cancel_get_404_invalid_token(self, api_client):
        """GET guest-cancel/<bad-token> returns 404."""
        response = api_client.get(f"{API}/bookings/guest-cancel/{uuid.uuid4()}/")
        assert response.status_code == 404

    def test_guest_cancel_get_400_when_booking_not_active(self, api_client):
        """GET guest-cancel/<token> returns 400 when booking is already cancelled."""
        business = BusinessFactory(isActive=True, verificationStatus="verified")
        klass = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=klass, cancellationPolicy="flexible")
        schedule = ScheduleFactory(option=option)
        instance = ScheduleInstanceFactory(
            schedule=schedule,
            date=date.today() + timedelta(days=14),
            time=time(10, 0),
            status="scheduled",
        )
        contact = ContactFactory(business=business)
        token = uuid.uuid4()
        Booking.objects.create(
            schedule_instance=instance,
            contact=contact,
            user=None,
            participants=1,
            amount_paid=Decimal("25.00"),
            payment_status="paid",
            status="cancelled",
            enrollment_type="Single Session",
            cancellation_policy="flexible",
            cancellation_token=token,
        )
        response = api_client.get(f"{API}/bookings/guest-cancel/{token}/")
        assert response.status_code == 400

    @patch("quickstart.views.public.guest_booking_views.send_booking_cancellation_user_email")
    @patch("quickstart.views.public.guest_booking_views.send_business_student_cancellation_email")
    def test_guest_cancel_post_cancels_booking(
        self, mock_business_email, mock_user_email, api_client
    ):
        """POST guest-cancel/<token> cancels booking when policy allows."""
        business = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            cancellationNotification=False,
        )
        klass = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=klass, cancellationPolicy="flexible")
        schedule = ScheduleFactory(option=option)
        instance = ScheduleInstanceFactory(
            schedule=schedule,
            date=date.today() + timedelta(days=14),
            time=time(10, 0),
            status="scheduled",
        )
        contact = ContactFactory(business=business)
        token = uuid.uuid4()
        booking = Booking.objects.create(
            schedule_instance=instance,
            contact=contact,
            user=None,
            participants=1,
            amount_paid=Decimal("25.00"),
            payment_status="paid",
            status="confirmed",
            enrollment_type="Single Session",
            cancellation_policy="flexible",
            cancellation_token=token,
        )
        response = api_client.post(f"{API}/bookings/guest-cancel/{token}/")
        assert response.status_code == 200
        booking.refresh_from_db()
        assert booking.status == "cancelled"
        assert booking.cancellation_token is None

    @patch("quickstart.views.public.guest_booking_views.send_booking_cancellation_user_email")
    @patch("quickstart.views.public.guest_booking_views.send_business_student_cancellation_email")
    def test_guest_cancel_post_strict_policy_rejected(
        self, mock_business_email, mock_user_email, api_client
    ):
        business = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            cancellationNotification=False,
        )
        klass = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=klass, cancellationPolicy="strict")
        schedule = ScheduleFactory(option=option)
        instance = ScheduleInstanceFactory(
            schedule=schedule,
            date=date.today() + timedelta(days=14),
            time=time(10, 0),
            status="scheduled",
        )
        contact = ContactFactory(business=business)
        token = uuid.uuid4()
        Booking.objects.create(
            schedule_instance=instance,
            contact=contact,
            user=None,
            participants=1,
            amount_paid=Decimal("25.00"),
            payment_status="paid",
            status="confirmed",
            enrollment_type="Single Session",
            cancellation_policy="strict",
            cancellation_token=token,
        )
        response = api_client.post(f"{API}/bookings/guest-cancel/{token}/")
        assert response.status_code == 400

    @patch("quickstart.views.public.guest_booking_views.send_booking_cancellation_user_email")
    @patch("quickstart.views.public.guest_booking_views.send_business_student_cancellation_email")
    @patch("quickstart.views.public.guest_booking_views.timezone.now")
    def test_guest_cancel_post_24h_policy_insufficient_notice(
        self, mock_now, mock_business_email, mock_user_email, api_client
    ):
        mock_now.return_value = django_timezone.make_aware(
            datetime(2030, 6, 1, 14, 0), timezone=pytz.UTC
        )
        business = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            business_timezone="America/Toronto",
            cancellationNotification=False,
        )
        klass = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=klass, cancellationPolicy="24h")
        schedule = ScheduleFactory(option=option)
        instance = ScheduleInstanceFactory(
            schedule=schedule,
            date=date(2030, 6, 1),
            time=time(20, 0),
            status="scheduled",
        )
        contact = ContactFactory(business=business)
        token = uuid.uuid4()
        Booking.objects.create(
            schedule_instance=instance,
            contact=contact,
            user=None,
            participants=1,
            amount_paid=Decimal("25.00"),
            payment_status="paid",
            status="confirmed",
            enrollment_type="Single Session",
            cancellation_policy="24h",
            cancellation_token=token,
        )
        response = api_client.post(f"{API}/bookings/guest-cancel/{token}/")
        assert response.status_code == 400

    @patch("quickstart.views.public.guest_booking_views.send_booking_cancellation_user_email")
    @patch("quickstart.views.public.guest_booking_views.send_business_student_cancellation_email")
    def test_guest_cancel_post_class_already_started(
        self, mock_business_email, mock_user_email, api_client
    ):
        business = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            cancellationNotification=False,
        )
        klass = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=klass, cancellationPolicy="flexible")
        schedule = ScheduleFactory(option=option)
        instance = ScheduleInstanceFactory(
            schedule=schedule,
            date=date.today() - timedelta(days=1),
            time=time(10, 0),
            status="scheduled",
        )
        contact = ContactFactory(business=business)
        token = uuid.uuid4()
        Booking.objects.create(
            schedule_instance=instance,
            contact=contact,
            user=None,
            participants=1,
            amount_paid=Decimal("25.00"),
            payment_status="paid",
            status="confirmed",
            enrollment_type="Single Session",
            cancellation_policy="flexible",
            cancellation_token=token,
        )
        response = api_client.post(f"{API}/bookings/guest-cancel/{token}/")
        assert response.status_code == 400


# -----------------------------------------------------------------------------
# Gift card (validate, purchase-intent)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestGiftCardEndpoints:
    def test_validate_gift_card_success(self, api_client):
        """POST gift-cards/validate/ with valid code returns balance."""
        gc = GiftCardFactory(current_balance=Decimal("50.00"), is_active=True)
        response = api_client.post(
            f"{API}/gift-cards/validate/",
            {"code": gc.code},
            format="json",
        )
        assert response.status_code == 200
        data = response.json()
        assert "balance" in data
        assert Decimal(str(data["balance"])) == Decimal("50.00")

    def test_validate_gift_card_invalid_code_404(self, api_client):
        """POST gift-cards/validate/ with invalid code returns 404."""
        response = api_client.post(
            f"{API}/gift-cards/validate/",
            {"code": "INVALID-CODE-1234"},
            format="json",
        )
        assert response.status_code == 404
        assert "error" in response.json()

    def test_validate_gift_card_zero_balance_400(self, api_client):
        """POST gift-cards/validate/ with zero balance returns 400."""
        gc = GiftCardFactory(current_balance=Decimal("0.00"), is_active=True)
        response = api_client.post(
            f"{API}/gift-cards/validate/",
            {"code": gc.code},
            format="json",
        )
        assert response.status_code == 400
        assert "error" in response.json()

    @patch("quickstart.views.public.public_giftcard_views.stripe.PaymentIntent.create")
    def test_gift_card_purchase_intent_returns_client_secret(self, mock_create, api_client):
        """POST gift-cards/purchase-intent/ returns clientSecret when payload valid."""
        mock_create.return_value = MagicMock(client_secret="pi_secret_xxx")
        response = api_client.post(
            f"{API}/gift-cards/purchase-intent/",
            {
                "amount": 50,
                "recipient_email": "r@example.com",
                "recipient_name": "Recipient",
                "sender_name": "Sender",
            },
            format="json",
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("clientSecret") == "pi_secret_xxx"
        assert data.get("amount") == 50

    def test_gift_card_purchase_intent_invalid_payload_400(self, api_client):
        """POST gift-cards/purchase-intent/ with invalid data returns 400."""
        response = api_client.post(
            f"{API}/gift-cards/purchase-intent/",
            {"amount": -1},
            format="json",
        )
        assert response.status_code == 400


# -----------------------------------------------------------------------------
# Auth (public entry points)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthPublicEndpoints:
    def test_login_requires_credentials(self, api_client):
        """POST login/ with no/invalid credentials returns 401 or 400."""
        response = api_client.post(
            f"{API}/login/",
            {},
            format="json",
        )
        assert response.status_code in (400, 401)

    def test_login_success_returns_user_and_sets_cookies(self, api_client, user):
        """POST login/ with valid credentials returns user; tokens are in HttpOnly cookies."""
        user.set_password("testpass123")
        user.save()
        response = api_client.post(
            f"{API}/login/",
            {"email": user.email, "password": "testpass123"},
            format="json",
        )
        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert data["user"].get("email") == user.email
        # Tokens are in HttpOnly cookies (Set-Cookie), not in response body

    def test_register_accepts_valid_payload(self, api_client):
        """POST auth/registration/ with valid data creates user (or 400 if validation)."""
        response = api_client.post(
            f"{API}/auth/registration/",
            {
                "email": "newuser@example.com",
                "password1": "Str0ng!Pass",
                "password2": "Str0ng!Pass",
                "username": "newuser@example.com",
                "first_name": "New",
                "last_name": "User",
                "country": "Canada",
                "city": "Toronto",
                "state": "ON",
                "address": "123 St",
                "zipCode": "M5V 1A1",
            },
            format="json",
        )
        assert response.status_code in (200, 201, 204, 400)

    def test_password_reset_accepts_email(self, api_client):
        """POST auth/password/reset/ accepts email (may return 200 even for unknown)."""
        response = api_client.post(
            f"{API}/auth/password/reset/",
            {"email": "noreply@example.com"},
            format="json",
        )
        assert response.status_code in (200, 204, 400)


# -----------------------------------------------------------------------------
# Widget (public with X-Business-ID = widget_api_key)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestWidgetEndpoints:
    def test_widget_config_requires_business_header(self, api_client):
        """GET widget/v1/config/ without X-Business-ID returns 401 or 403."""
        response = api_client.get(f"{API}/widget/v1/config/")
        assert response.status_code in (401, 403, 404)

    def test_widget_config_returns_config_with_valid_key(self, api_client, business):
        """GET widget/v1/config/ with valid X-Business-ID returns 200 and config."""
        response = api_client.get(
            f"{API}/widget/v1/config/",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 200
        data = response.json()
        assert "stripe_publishable_key" in data or "businessName" in data or "slug" in data

    def test_widget_classes_list_returns_200_with_valid_key(
        self, api_client, business, public_class
    ):
        """GET widget/v1/classes/ with valid key returns class list."""
        response = api_client.get(
            f"{API}/widget/v1/classes/",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 200

    def test_widget_availability_requires_params(self, api_client, business):
        """GET widget/v1/availability/ without option_id/dates returns 400."""
        response = api_client.get(
            f"{API}/widget/v1/availability/",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 400

    def test_widget_events_post_204_with_valid_key(self, api_client, business):
        """POST widget/v1/events/ with valid X-Business-ID returns 204."""
        response = api_client.post(
            f"{API}/widget/v1/events/",
            {"event": "loaded"},
            format="json",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 204

    def test_widget_validate_coupon_missing_params_400(self, api_client, business):
        """POST widget/v1/validate-coupon/ with missing params returns 400."""
        response = api_client.post(
            f"{API}/widget/v1/validate-coupon/",
            {},
            format="json",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 400


# -----------------------------------------------------------------------------
# Class reviews (public paginated)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestClassReviewsPaginated:
    def test_class_reviews_returns_200(self, api_client, public_class):
        """GET classes/<identifier>/reviews/ returns 200."""
        response = api_client.get(f"{API}/classes/{public_class.slug}/reviews/")
        assert response.status_code == 200


# -----------------------------------------------------------------------------
# Payments & booking (public/guest); webhook rejects invalid payload
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestPaymentEndpoints:
    @patch("quickstart.payments.views.stripe.PaymentIntent.create")
    def test_create_payment_intent_requires_valid_payload(self, mock_create, api_client):
        """POST payments/create-payment-intent/ with missing data returns 400."""
        response = api_client.post(
            f"{API}/payments/create-payment-intent/",
            {},
            format="json",
        )
        assert response.status_code == 400

    def test_update_payment_intent_requires_payment_intent_id(self, api_client):
        """POST payments/update-payment-intent/ without payment_intent_id returns 400."""
        response = api_client.post(
            f"{API}/payments/update-payment-intent/",
            {},
            format="json",
        )
        assert response.status_code == 400

    def test_cancel_payment_intent_requires_payment_intent_id(self, api_client):
        """POST payments/cancel-payment-intent/ without payment_intent_id returns 400."""
        response = api_client.post(
            f"{API}/payments/cancel-payment-intent/",
            {},
            format="json",
        )
        assert response.status_code == 400

    def test_booking_webhook_invalid_signature_400(self, api_client):
        """POST payments/webhook/ without valid Stripe signature returns 400."""
        response = api_client.post(
            f"{API}/payments/webhook/",
            b"{}",
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_booking_status_without_client_secret_guest_400(self, api_client):
        """GET booking-status/by-payment-intent/<id>/ as guest without client_secret returns 400."""
        response = api_client.get(
            f"{API}/booking-status/by-payment-intent/pi_fake_123/"
        )
        assert response.status_code in (400, 401, 404)


# -----------------------------------------------------------------------------
# Widget: payment-intent and bookings (require X-Business-ID; validate payload)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestWidgetPaymentAndBookings:
    def test_widget_payment_intent_requires_valid_payload(
        self, api_client, business
    ):
        """POST widget/v1/payment-intent/ with header but invalid payload returns 400."""
        response = api_client.post(
            f"{API}/widget/v1/payment-intent/",
            {},
            format="json",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code in (400, 404)

    def test_widget_bookings_create_requires_valid_payload(
        self, api_client, business
    ):
        """POST widget/v1/bookings/ with header but invalid payload returns 400."""
        response = api_client.post(
            f"{API}/widget/v1/bookings/",
            {},
            format="json",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code in (400, 404)
