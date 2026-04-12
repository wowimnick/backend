"""Tests for quickstart.services.subscription_sync (Stripe object dict mocks)."""
import pytest
from django.db import DatabaseError

from quickstart.models import (
    ADDON_TYPE_EMAIL_MARKETING,
    ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
    BusinessAddonSubscription,
    WidgetSubscription,
)
from quickstart.services.subscription_sync import (
    mark_addon_subscription_canceled,
    mark_widget_subscription_canceled,
    sync_addon_subscription_from_stripe,
    sync_widget_subscription_from_stripe,
)
from quickstart.tests.stripe_mocks import make_stripe_schedule


@pytest.fixture(autouse=True)
def _widget_price_map(settings):
    settings.WIDGET_SUBSCRIPTION_PRICE_BASIC = "price_basic"
    settings.WIDGET_SUBSCRIPTION_PRICE_GROWTH = "price_growth"
    settings.WIDGET_SUBSCRIPTION_PRICE_ADVANCED = "price_advanced"


def _widget_sub_dict(business, **kwargs):
    base = {
        "id": "sub_sync_test",
        "status": "active",
        "metadata": {"business_id": str(business.businessId), "plan_id": "basic"},
        "items": {
            "data": [{"id": "si_1", "price": {"id": "price_basic"}}],
        },
        "customer": "cus_sync",
        "current_period_end": 2_000_000_000,
        "cancel_at_period_end": False,
    }
    base.update(kwargs)
    return base


@pytest.mark.django_db
class TestSyncWidgetSubscription:
    def test_sync_creates_new_row_from_stripe_object(self, business):
        sub = _widget_sub_dict(business)
        row, err = sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub)
        assert err is None
        assert row is not None
        assert row.business_id == business.pk
        assert row.stripe_subscription_id == "sub_sync_test"
        assert row.plan_id == "basic"

    def test_sync_updates_existing_row(self, business):
        sub = _widget_sub_dict(business)
        sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub)
        sub2 = _widget_sub_dict(
            business,
            status="past_due",
            items={"data": [{"id": "si_1", "price": {"id": "price_growth"}}]},
        )
        row, err = sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub2)
        assert err is None
        assert row.plan_id == "growth"
        assert row.status == "past_due"

    def test_sync_maps_price_id_to_plan_id(self, business):
        sub = _widget_sub_dict(
            business,
            items={"data": [{"id": "si_1", "price": {"id": "price_advanced"}}]},
        )
        row, err = sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub)
        assert err is None
        assert row.plan_id == "advanced"

    def test_sync_falls_back_to_metadata_plan_id_when_price_unknown(self, business):
        sub = _widget_sub_dict(
            business,
            items={"data": []},
            metadata={"business_id": str(business.businessId), "plan_id": "advanced"},
        )
        row, err = sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub)
        assert err is None
        assert row.plan_id == "advanced"

    def test_sync_defaults_to_growth_on_unknown_metadata_plan(self, business):
        sub = _widget_sub_dict(
            business,
            items={"data": []},
            metadata={"business_id": str(business.businessId), "plan_id": "not_a_plan"},
        )
        row, err = sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub)
        assert err is None
        assert row.plan_id == "growth"

    @pytest.mark.parametrize(
        "schedule_obj",
        [
            {
                "end_behavior": "cancel",
                "phases": [{"start_date": 1, "end_date": 2}],
            },
            make_stripe_schedule(
                end_behavior="cancel",
                phases=[{"start_date": 1, "end_date": 2}],
            ),
        ],
    )
    def test_sync_detects_cancel_from_schedule_end_behavior(self, business, schedule_obj):
        """Expanded schedule on subscription sets cancel_at_period_end when end_behavior=cancel and one phase."""
        sub = _widget_sub_dict(
            business,
            schedule=schedule_obj,
        )
        row, err = sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub)
        assert err is None
        assert row.cancel_at_period_end is True

    def test_sync_sets_stripe_customer_id_on_business(self, business):
        business.stripe_customer_id = ""
        business.save(update_fields=["stripe_customer_id"])
        sub = _widget_sub_dict(business, customer="cus_new_from_sub")
        sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub)
        business.refresh_from_db()
        assert business.stripe_customer_id == "cus_new_from_sub"

    def test_sync_handles_missing_business_id(self):
        sub = {
            "id": "sub_bad_meta",
            "status": "active",
            "metadata": {},
            "items": {"data": []},
            "customer": "cus_x",
        }
        row, err = sync_widget_subscription_from_stripe("sub_bad_meta", subscription_obj=sub)
        assert row is None
        assert err is not None

    def test_sync_handles_nonexistent_business(self):
        sub = {
            "id": "sub_no_biz",
            "status": "active",
            "metadata": {"business_id": "999999999"},
            "items": {"data": []},
            "customer": "cus_x",
        }
        row, err = sync_widget_subscription_from_stripe("sub_no_biz", subscription_obj=sub)
        assert row is None
        assert "not found" in (err or "").lower()

    def test_sync_handles_concurrent_save_database_error(self, business, monkeypatch):
        sub = _widget_sub_dict(business)
        sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub)
        sub2 = _widget_sub_dict(business, status="canceled")

        real_save = WidgetSubscription.save
        calls = {"n": 0}

        def flaky_save(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DatabaseError("simulated concurrent failure")
            return real_save(self, *a, **kw)

        monkeypatch.setattr(WidgetSubscription, "save", flaky_save)
        row, err = sync_widget_subscription_from_stripe("sub_sync_test", subscription_obj=sub2)
        assert err is None
        assert row.status == "canceled"


@pytest.mark.django_db
class TestSyncAddonSubscription:
    def _addon_sub(self, business, addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING, **kwargs):
        base = {
            "id": "sub_addon_sync",
            "status": "active",
            "metadata": {"business_id": str(business.businessId)},
            "items": {"data": [{"price": {"id": "price_addon"}}]},
            "customer": "cus_addon",
            "current_period_start": 1_900_000_000,
            "current_period_end": 2_000_000_000,
            "cancel_at_period_end": False,
        }
        base.update(kwargs)
        return base

    def test_sync_addon_creates_row(self, business):
        sub = self._addon_sub(business)
        row, err = sync_addon_subscription_from_stripe(
            "sub_addon_sync", subscription_obj=sub, addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
        )
        assert err is None
        assert row.addon_type == ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
        assert BusinessAddonSubscription.objects.filter(stripe_subscription_id="sub_addon_sync").exists()

    def test_sync_addon_updates_existing(self, business):
        sub = self._addon_sub(business, status="active")
        sync_addon_subscription_from_stripe(
            "sub_addon_sync", subscription_obj=sub, addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
        )
        sub2 = self._addon_sub(business, status="past_due")
        row, err = sync_addon_subscription_from_stripe(
            "sub_addon_sync", subscription_obj=sub2, addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
        )
        assert err is None
        assert row.status == "past_due"

    def test_sync_addon_enables_marketplace_email_branding_on_business(self, business):
        business.marketplace_email_branding_enabled = False
        business.save(update_fields=["marketplace_email_branding_enabled"])
        sub = self._addon_sub(business, status="active")
        sync_addon_subscription_from_stripe(
            "sub_addon_sync", subscription_obj=sub, addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
        )
        business.refresh_from_db()
        assert business.marketplace_email_branding_enabled is True

    def test_sync_addon_enables_email_marketing_on_business(self, business):
        business.email_marketing_enabled = False
        business.save(update_fields=["email_marketing_enabled"])
        sub = self._addon_sub(business, status="trialing")
        sync_addon_subscription_from_stripe(
            "sub_em_sync",
            subscription_obj={**sub, "id": "sub_em_sync"},
            addon_type=ADDON_TYPE_EMAIL_MARKETING,
        )
        business.refresh_from_db()
        assert business.email_marketing_enabled is True

    def test_sync_addon_disables_flag_when_canceled(self, business):
        business.marketplace_email_branding_enabled = True
        business.save(update_fields=["marketplace_email_branding_enabled"])
        BusinessAddonSubscription.objects.create(
            business=business,
            addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
            stripe_subscription_id="sub_addon_off",
            status="active",
        )
        sub = self._addon_sub(business, status="canceled")
        sub["id"] = "sub_addon_off"
        sync_addon_subscription_from_stripe(
            "sub_addon_off", subscription_obj=sub, addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
        )
        business.refresh_from_db()
        assert business.marketplace_email_branding_enabled is False


@pytest.mark.django_db
class TestMarkCanceled:
    def test_mark_widget_subscription_canceled(self, business):
        WidgetSubscription.objects.create(
            business=business,
            stripe_subscription_id="sub_mark_w",
            plan_id="basic",
            status="active",
        )
        n = mark_widget_subscription_canceled("sub_mark_w")
        assert n == 1
        assert (
            WidgetSubscription.objects.get(stripe_subscription_id="sub_mark_w").status
            == "canceled"
        )

    def test_mark_addon_subscription_canceled_disables_marketplace_flag(self, business):
        business.marketplace_email_branding_enabled = True
        business.save(update_fields=["marketplace_email_branding_enabled"])
        BusinessAddonSubscription.objects.create(
            business=business,
            addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
            stripe_subscription_id="sub_mark_a",
            status="active",
        )
        mark_addon_subscription_canceled("sub_mark_a")
        business.refresh_from_db()
        assert business.marketplace_email_branding_enabled is False

    def test_mark_addon_subscription_canceled_disables_email_marketing_flag(self, business):
        business.email_marketing_enabled = True
        business.save(update_fields=["email_marketing_enabled"])
        BusinessAddonSubscription.objects.create(
            business=business,
            addon_type=ADDON_TYPE_EMAIL_MARKETING,
            stripe_subscription_id="sub_mark_em",
            status="active",
        )
        mark_addon_subscription_canceled("sub_mark_em")
        business.refresh_from_db()
        assert business.email_marketing_enabled is False

    def test_mark_canceled_returns_zero_when_not_found(self):
        assert mark_widget_subscription_canceled("sub_missing") == 0
