"""Service layer unit tests."""
import pytest

from quickstart.models import WidgetSubscription
from quickstart.services.subscription_sync import (
    _widget_price_to_plan_id,
    sync_widget_subscription_from_stripe,
)
from quickstart.utils.marketing_html import sanitize_marketing_html


def test_widget_price_to_plan_id_handles_empty():
    assert _widget_price_to_plan_id(None) is None
    assert _widget_price_to_plan_id("") is None


def test_sanitize_marketing_html_strips_script():
    html = '<p>Hi</p><script>alert(1)</script>'
    out = sanitize_marketing_html(html)
    assert "<script>" not in out.lower()


@pytest.mark.django_db
def test_sync_widget_subscription_from_dict_creates_row(business):
    sub = {
        "id": "sub_unit_test_sync",
        "status": "active",
        "metadata": {"business_id": str(business.businessId), "plan_id": "basic"},
        "items": {"data": []},
        "customer": "cus_test",
    }
    row, err = sync_widget_subscription_from_stripe("sub_unit_test_sync", subscription_obj=sub)
    assert err is None
    assert row is not None
    assert WidgetSubscription.objects.filter(business=business).exists()
