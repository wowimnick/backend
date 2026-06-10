"""Tests for scam filter JSON parsing, IP ban middleware, and lock/refresh enforcement."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from quickstart.middleware import BannedIPMiddleware
from quickstart.models import AuditLog, BannedIP, ConversationMessage
from quickstart.tests.factories import ConversationFactory, ConversationMessageFactory, UserFactory
from quickstart.utils.scam_filter_ai import parse_scam_classifier_json

API = "/api"


class TestScamFilterJsonParsing:
    def test_parses_valid_json(self):
        raw = '{"is_scam": true, "confidence": 0.95, "reason": "SEO spam"}'
        parsed = parse_scam_classifier_json(raw)
        assert parsed is not None
        assert parsed["is_scam"] is True
        assert parsed["confidence"] == 0.95
        assert parsed["reason"] == "SEO spam"

    def test_parses_markdown_fenced_json(self):
        raw = '```json\n{"is_scam": false, "confidence": 0.1, "reason": "Genuine inquiry"}\n```'
        parsed = parse_scam_classifier_json(raw)
        assert parsed is not None
        assert parsed["is_scam"] is False

    def test_regex_fallback_for_malformed_json(self):
        raw = "{'is_scam': true, 'confidence': 0.8, 'reason': 'Phishing'}"
        parsed = parse_scam_classifier_json(raw)
        assert parsed is not None
        assert parsed["is_scam"] is True


@pytest.mark.django_db
class TestBannedIPMiddleware:
    def test_banned_ip_returns_403_on_api(self):
        BannedIP.objects.create(ip_address="203.0.113.50", reason="test", is_active=True)

        factory = RequestFactory()
        request = factory.get("/api/conversations/")
        request.META["REMOTE_ADDR"] = "203.0.113.50"

        middleware = BannedIPMiddleware(lambda req: MagicMock(status_code=200))
        response = middleware(request)
        assert response.status_code == 403

    def test_non_banned_ip_passes_through(self):
        factory = RequestFactory()
        request = factory.get("/api/conversations/")
        request.META["REMOTE_ADDR"] = "198.51.100.10"

        downstream = MagicMock(status_code=200)
        middleware = BannedIPMiddleware(lambda req: downstream)
        response = middleware(request)
        assert response is downstream


@pytest.mark.django_db
class TestBanAccountIps:
    def test_ban_account_ips_creates_banned_ip_records(self, admin_client):
        user = UserFactory()
        AuditLog.objects.create(
            user=user,
            user_email=user.email,
            action="login",
            details="Login",
            ip_address="203.0.113.77",
        )

        response = admin_client.post(
            f"{API}/admin/users/{user.userId}/ban_account_ips/"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["banned_ips"] == ["203.0.113.77"]
        assert BannedIP.objects.filter(
            ip_address="203.0.113.77", is_active=True
        ).exists()

        list_response = admin_client.get(f"{API}/admin/banned-ips/")
        assert list_response.status_code == status.HTTP_200_OK
        results = list_response.data.get("results", list_response.data)
        assert any(row["ip_address"] == "203.0.113.77" for row in results)

    def test_ban_account_ips_without_login_history_returns_400(self, admin_client):
        user = UserFactory()

        response = admin_client.post(
            f"{API}/admin/users/{user.userId}/ban_account_ips/"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "No IP addresses found" in response.data["detail"]

    def test_ban_account_ips_uses_message_sender_ip(self, admin_client):
        user = UserFactory()
        conv = ConversationFactory(booker_user=user)
        ConversationMessageFactory(
            conversation=conv,
            sender_user=user,
            sender_type="booker",
            sender_ip="198.51.100.22",
            text="Hello",
        )

        response = admin_client.post(
            f"{API}/admin/users/{user.userId}/ban_account_ips/"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["banned_ips"] == ["198.51.100.22"]

    def test_ban_account_ips_matches_login_logs_by_email(self, admin_client):
        user = UserFactory()
        AuditLog.objects.create(
            user=None,
            user_email=user.email,
            action="login",
            details="Legacy login log",
            ip_address="203.0.113.88",
        )

        response = admin_client.post(
            f"{API}/admin/users/{user.userId}/ban_account_ips/"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data["banned_ips"] == ["203.0.113.88"]


@pytest.mark.django_db
class TestLockAndRefreshEnforcement:
    def test_refresh_rejects_inactive_user(self, settings):
        user = UserFactory(is_active=False)
        refresh = RefreshToken.for_user(user)
        client = APIClient()
        settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"] = "refresh_token"
        client.cookies["refresh_token"] = str(refresh)

        response = client.post(f"{API}/token/refresh/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.data.get("detail") == "Account is disabled."

    def test_lock_account_blacklists_outstanding_tokens(self, admin_client):
        user = UserFactory(is_active=True)
        refresh = RefreshToken.for_user(user)
        assert refresh.token is not None

        response = admin_client.post(f"{API}/admin/users/{user.userId}/lock_account/")
        assert response.status_code == status.HTTP_200_OK

        user.refresh_from_db()
        assert user.is_active is False

        client = APIClient()
        from django.conf import settings as dj_settings

        client.cookies[dj_settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"]] = str(refresh)
        refresh_response = client.post(f"{API}/token/refresh/")
        assert refresh_response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestBusinessHidesQuarantinedMessages:
    def test_pending_booker_message_hidden_from_business_list(
        self, business_owner_client, business
    ):
        conv = ConversationFactory(business=business)
        ConversationMessageFactory(
            conversation=conv,
            text="Visible approved message",
            moderation_status=ConversationMessage.MODERATION_APPROVED,
        )
        ConversationMessageFactory(
            conversation=conv,
            text="Hidden pending spam",
            moderation_status=ConversationMessage.MODERATION_PENDING,
        )

        response = business_owner_client.get(f"{API}/business/conversations/")
        assert response.status_code == status.HTTP_200_OK
        conv_data = next((c for c in response.data if c["id"] == str(conv.id)), None)
        assert conv_data is not None
        assert conv_data["last_message_preview"] == "Visible approved message"
