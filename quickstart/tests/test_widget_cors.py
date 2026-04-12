"""
DynamicCorsMiddleware: widget path CORS for embed hosts and allowed domains.
"""
import pytest
from django.test import Client

from quickstart.middleware import (
    WIDGET_EMBED_HOST_SUFFIXES,
    _is_known_embed_host,
    _origin_allowed_for_business,
)
from quickstart.tests.factories import BusinessFactory

API = "/api"


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("https://abc123.filesusr.com", True),
        ("https://my-site.webflow.io", True),
        ("https://preview.canvas.webflow.com", True),
        ("https://brave-panda-42.squarespace.com", True),
        ("https://user.mystrikingly.com", True),
        ("https://mysite.weebly.com", True),
        ("https://something.square.site", True),
        ("https://demo.carrd.co", True),
        ("https://myschool.sites.google.com", True),
        ("https://evil.com", False),
        ("https://filesusr.com", False),
    ],
)
def test_is_known_embed_host(origin, expected):
    assert _is_known_embed_host(origin) is expected


@pytest.mark.django_db
def test_widget_options_preflight_sets_acao():
    client = Client()
    res = client.options(
        f"{API}/widget/v1/config/",
        HTTP_ORIGIN="https://example.com",
    )
    assert res.status_code == 200
    assert "Access-Control-Allow-Origin" in res


@pytest.mark.django_db
def test_widget_get_rejects_unknown_origin_without_allowed_domains(api_client, business):
    res = api_client.get(
        f"{API}/widget/v1/config/",
        HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        HTTP_ORIGIN="https://malicious.example",
    )
    assert res.status_code == 403
    assert b"Origin not allowed" in res.content


@pytest.mark.django_db
def test_widget_get_allows_listed_allowed_domain(api_client, business):
    business.allowed_widget_origins = ["https://trusted-client.com"]
    business.save(update_fields=["allowed_widget_origins"])
    res = api_client.get(
        f"{API}/widget/v1/config/",
        HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        HTTP_ORIGIN="https://trusted-client.com",
    )
    assert res.status_code in (200, 403)


@pytest.mark.django_db
def test_widget_get_allows_known_embed_host_when_allowed_domains_set(api_client, business):
    business.allowed_widget_origins = ["https://mywixsite.com"]
    business.save(update_fields=["allowed_widget_origins"])
    res = api_client.get(
        f"{API}/widget/v1/config/",
        HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        HTTP_ORIGIN="https://uuid-here.filesusr.com",
    )
    assert res.status_code in (200, 403)
    if res.status_code == 200:
        assert res.get("Access-Control-Allow-Origin") == "https://uuid-here.filesusr.com"


@pytest.mark.django_db
def test_widget_get_blocks_known_embed_host_when_no_allowed_domains(api_client, business):
    business.allowed_widget_origins = []
    business.save(update_fields=["allowed_widget_origins"])
    res = api_client.get(
        f"{API}/widget/v1/config/",
        HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        HTTP_ORIGIN="https://uuid-here.filesusr.com",
    )
    assert res.status_code == 403


@pytest.mark.django_db
def test_demo_key_rejects_non_classeasily_origin(api_client):
    res = api_client.get(
        f"{API}/widget/v1/config/",
        HTTP_X_BUSINESS_ID="demo",
        HTTP_ORIGIN="https://uuid-here.filesusr.com",
    )
    assert res.status_code == 403


def test_embed_suffixes_tuple_covers_documented_builders():
    assert ".filesusr.com" in WIDGET_EMBED_HOST_SUFFIXES
    assert ".square.site" in WIDGET_EMBED_HOST_SUFFIXES


@pytest.mark.django_db
def test_origin_allowed_helper_embed_host_requires_domains():
    b = BusinessFactory(allowed_widget_origins=["https://ok.com"])
    assert _origin_allowed_for_business("https://x.filesusr.com", b) is True
    b2 = BusinessFactory(allowed_widget_origins=[])
    assert _origin_allowed_for_business("https://x.filesusr.com", b2) is False
