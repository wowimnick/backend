"""Middleware behavior (lightweight request checks)."""
import pytest
from django.test import Client


@pytest.mark.django_db
def test_health_check_middleware_root_fast():
    c = Client()
    r = c.get("/health-check/")
    assert r.status_code == 200


@pytest.mark.django_db
def test_api_health_check():
    c = Client()
    r = c.get("/api/health-check/")
    assert r.status_code == 200
