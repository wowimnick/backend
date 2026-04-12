"""Unit tests for quickstart.services.widget_subscription_service (Stripe mocked)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe
from quickstart.models import WidgetSubscription
from quickstart.services.widget_subscription_service import (
    _get_price_id,
    _is_downgrade,
    cancel_at_period_end,
    create_subscription,
    downgrade_subscription,
    proration_after_subscription_item_modify,
    reactivate,
    same_price_open_invoice,
    upgrade_subscription,
)
from quickstart.tests.factories import WidgetSubscriptionFactory
from quickstart.tests.stripe_mocks import (
    make_service_schedule,
    make_service_subscription,
    make_stripe_invoice,
    make_stripe_list,
    make_stripe_payment_intent,
    make_stripe_subscription,
)


@pytest.fixture(autouse=True)
def _default_widget_prices(settings):
    """Most tests expect per-plan widget Stripe price IDs configured."""
    settings.WIDGET_SUBSCRIPTION_PRICE_BASIC = "price_basic"
    settings.WIDGET_SUBSCRIPTION_PRICE_GROWTH = "price_growth"
    settings.WIDGET_SUBSCRIPTION_PRICE_ADVANCED = "price_advanced"


class TestHelpers:
    """Pure helpers; no DB."""

    def test_is_downgrade_growth_to_basic(self):
        assert _is_downgrade("growth", "basic") is True

    def test_is_downgrade_basic_to_growth(self):
        assert _is_downgrade("basic", "growth") is False

    def test_is_downgrade_same_plan(self):
        assert _is_downgrade("growth", "growth") is False

    def test_is_downgrade_invalid_plan(self):
        assert _is_downgrade("invalid", "basic") is False


@pytest.mark.django_db
class TestGetPriceId:
    def test_get_price_id_returns_configured_price(self, settings):
        settings.WIDGET_SUBSCRIPTION_PRICE_BASIC = "price_basic"
        settings.WIDGET_SUBSCRIPTION_PRICE_GROWTH = "price_growth"
        settings.WIDGET_SUBSCRIPTION_PRICE_ADVANCED = "price_advanced"
        assert _get_price_id("basic") == "price_basic"
        assert _get_price_id("growth") == "price_growth"
        assert _get_price_id("advanced") == "price_advanced"


@pytest.mark.django_db
def test_get_price_id_returns_none_when_unconfigured(settings):
    settings.WIDGET_SUBSCRIPTION_PRICE_BASIC = None
    settings.WIDGET_SUBSCRIPTION_PRICE_GROWTH = None
    settings.WIDGET_SUBSCRIPTION_PRICE_ADVANCED = None
    settings.WIDGET_SUBSCRIPTION_PRICE_ID = None
    assert _get_price_id("basic") is None


@pytest.mark.django_db
class TestCreateSubscription:
    @patch("quickstart.services.widget_subscription_service.stripe.PaymentIntent.retrieve")
    @patch("quickstart.services.widget_subscription_service._get_default_payment_method_id", return_value=None)
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.create")
    @patch("quickstart.services.widget_subscription_service.stripe.Customer.create")
    def test_create_subscription_new_customer(
        self, mock_customer_create, mock_sub_create, _mock_pm, mock_pi_retrieve, business
    ):
        business.stripe_customer_id = ""
        business.save(update_fields=["stripe_customer_id"])
        mock_customer_create.return_value = SimpleNamespace(id="cus_new")
        inv = make_stripe_invoice(
            payment_intent=make_stripe_payment_intent(status="requires_confirmation"),
        )
        mock_sub_create.return_value = SimpleNamespace(
            id="sub_new",
            status="incomplete",
            latest_invoice=inv,
            items=SimpleNamespace(
                data=[SimpleNamespace(price=SimpleNamespace(id="price_basic"))]
            ),
        )
        mock_pi_retrieve.return_value = make_stripe_payment_intent(status="requires_confirmation")

        stripe_sub, client_secret, row = create_subscription(business, "basic")

        mock_customer_create.assert_called_once()
        mock_sub_create.assert_called_once()
        assert stripe_sub.id == "sub_new"
        assert client_secret == "pi_test_secret"
        assert row.stripe_subscription_id == "sub_new"
        business.refresh_from_db()
        assert business.stripe_customer_id == "cus_new"

    @patch("quickstart.services.widget_subscription_service._get_default_payment_method_id", return_value=None)
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.create")
    def test_create_subscription_existing_customer(self, mock_sub_create, _mock_pm, business):
        business.stripe_customer_id = "cus_existing"
        business.save(update_fields=["stripe_customer_id"])
        inv = make_stripe_invoice(
            payment_intent=make_stripe_payment_intent(status="requires_confirmation"),
        )
        mock_sub_create.return_value = SimpleNamespace(
            id="sub_new2",
            status="incomplete",
            latest_invoice=inv,
            items=SimpleNamespace(
                data=[SimpleNamespace(price=SimpleNamespace(id="price_basic"))]
            ),
        )

        with patch(
            "quickstart.services.widget_subscription_service.stripe.Customer.create"
        ) as mock_cust:
            create_subscription(business, "basic")
            mock_cust.assert_not_called()

    @patch("quickstart.services.widget_subscription_service._get_default_payment_method_id", return_value=None)
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.create")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.cancel")
    def test_create_subscription_cancels_incomplete_existing(
        self, mock_cancel, mock_sub_create, _mock_pm, business
    ):
        business.stripe_customer_id = "cus_x"
        business.save(update_fields=["stripe_customer_id"])
        WidgetSubscription.objects.create(
            business=business,
            plan_id="basic",
            status="incomplete",
            stripe_subscription_id="sub_old_incomplete",
        )
        inv = make_stripe_invoice(
            payment_intent=make_stripe_payment_intent(status="requires_confirmation"),
        )
        mock_sub_create.return_value = SimpleNamespace(
            id="sub_fresh",
            status="incomplete",
            latest_invoice=inv,
            items=SimpleNamespace(
                data=[SimpleNamespace(price=SimpleNamespace(id="price_basic"))]
            ),
        )

        create_subscription(business, "growth")
        mock_cancel.assert_called_once_with("sub_old_incomplete")

    @patch("quickstart.services.widget_subscription_service._get_default_payment_method_id", return_value=None)
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.create")
    def test_create_subscription_returns_none_client_secret_when_paid(self, mock_sub_create, _mock_pm, business):
        business.stripe_customer_id = "cus_x"
        business.save(update_fields=["stripe_customer_id"])
        mock_sub_create.return_value = SimpleNamespace(
            id="sub_paid",
            status="active",
            latest_invoice=make_stripe_invoice(status="paid", payment_intent=None),
            items=SimpleNamespace(
                data=[SimpleNamespace(price=SimpleNamespace(id="price_basic"))]
            ),
        )

        _stripe_sub, client_secret, _row = create_subscription(business, "basic")
        assert client_secret is None

    def test_create_subscription_raises_on_unconfigured_price(self, settings, business):
        settings.WIDGET_SUBSCRIPTION_PRICE_BASIC = None
        settings.WIDGET_SUBSCRIPTION_PRICE_GROWTH = None
        settings.WIDGET_SUBSCRIPTION_PRICE_ADVANCED = None
        settings.WIDGET_SUBSCRIPTION_PRICE_ID = None
        with pytest.raises(ValueError, match="not configured"):
            create_subscription(business, "basic")


@pytest.mark.django_db
class TestUpgradeSubscription:
    def _business(self, business):
        business.stripe_customer_id = "cus_x"
        business.save(update_fields=["stripe_customer_id"])
        return WidgetSubscriptionFactory(
            business=business,
            plan_id="basic",
            status="active",
            stripe_subscription_id="sub_up",
            stripe_price_id="price_basic",
        )

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    def test_upgrade_basic_to_growth_immediate_proration(
        self, mock_inv_list, mock_retrieve, mock_modify, business
    ):
        sub = self._business(business)
        first = make_service_subscription(
            current_price_id="price_basic",
            sub_id="sub_up",
        )
        verified = make_service_subscription(
            current_price_id="price_growth",
            sub_id="sub_up",
            status="active",
            cancel_at_period_end=False,
        )
        mock_retrieve.side_effect = [first, verified]
        mock_modify.return_value = SimpleNamespace(
            id="sub_up",
            status="active",
            latest_invoice=make_stripe_invoice(status="paid"),
            cancel_at_period_end=False,
            items=first.items,
        )
        mock_inv_list.return_value = make_stripe_list([])

        requires_pay, secret, updated, err = upgrade_subscription(sub, "growth", business)

        assert err is None
        assert requires_pay is False
        assert secret is None
        assert updated.plan_id == "growth"
        assert updated.stripe_price_id == "price_growth"
        mock_modify.assert_called()
        call_kw = mock_modify.call_args_list[0].kwargs
        assert call_kw["proration_behavior"] == "always_invoice"
        assert call_kw["payment_behavior"] == "pending_if_incomplete"

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    def test_upgrade_basic_to_advanced(
        self, mock_inv_list, mock_retrieve, mock_modify, business
    ):
        sub = self._business(business)
        first = make_service_subscription(
            current_price_id="price_basic", sub_id="sub_up"
        )
        verified = make_service_subscription(
            current_price_id="price_advanced",
            sub_id="sub_up",
            status="active",
        )
        mock_retrieve.side_effect = [first, verified]
        mock_modify.return_value = SimpleNamespace(
            id="sub_up",
            status="active",
            latest_invoice=make_stripe_invoice(status="paid"),
            items=first.items,
        )
        mock_inv_list.return_value = make_stripe_list([])
        _req, _sec, updated, err = upgrade_subscription(sub, "advanced", business)
        assert err is None
        assert updated.plan_id == "advanced"

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    @patch("quickstart.services.widget_subscription_service.stripe.PaymentIntent.retrieve")
    def test_upgrade_returns_client_secret_when_proration_needs_payment(
        self, mock_pi_ret, mock_inv_list, mock_retrieve, mock_modify, business
    ):
        sub = self._business(business)
        mock_retrieve.return_value = make_service_subscription(
            current_price_id="price_basic", sub_id="sub_up"
        )
        inv = make_stripe_invoice(
            payment_intent=make_stripe_payment_intent(status="requires_confirmation"),
        )
        mock_modify.return_value = SimpleNamespace(
            id="sub_up",
            latest_invoice=inv,
            items=make_service_subscription(current_price_id="price_basic").items,
        )
        mock_inv_list.return_value = make_stripe_list([])
        mock_pi_ret.return_value = make_stripe_payment_intent()

        requires_pay, secret, updated, err = upgrade_subscription(sub, "growth", business)
        assert err is None
        assert requires_pay is True
        assert secret == "pi_test_secret"
        assert updated == sub

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_upgrade_fails_on_invalid_subscription_state(self, mock_retrieve, mock_modify, business):
        sub = self._business(business)
        mock_retrieve.return_value = SimpleNamespace(
            id="sub_up",
            items=SimpleNamespace(data=[]),
        )
        req, sec, updated, err = upgrade_subscription(sub, "growth", business)
        assert err == "Invalid subscription state."
        assert req is None
        mock_modify.assert_not_called()

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_upgrade_handles_stripe_error_gracefully(self, mock_retrieve, mock_modify, business):
        sub = self._business(business)
        mock_retrieve.return_value = make_service_subscription(current_price_id="price_basic")
        mock_modify.side_effect = stripe.StripeError("boom")
        req, sec, updated, err = upgrade_subscription(sub, "growth", business)
        assert "boom" in err
        assert req is None


@pytest.mark.django_db
class TestDowngradeSubscription:
    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.create")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_downgrade_growth_to_basic_creates_schedule(
        self, mock_sub_ret, mock_sched_create, mock_sched_modify, mock_sync, business
    ):
        business.stripe_customer_id = "cus_x"
        business.save(update_fields=["stripe_customer_id"])
        sub = WidgetSubscriptionFactory(
            business=business,
            plan_id="growth",
            status="active",
            stripe_subscription_id="sub_dg",
        )
        mock_sub_ret.return_value = make_service_subscription(
            sub_id="sub_dg",
            current_price_id="price_growth",
            period_end_ts=2_000_000_000,
        )
        mock_sched_create.return_value = SimpleNamespace(
            id="sched_new",
            phases=[{"start_date": 1_700_000_000, "items": [{"price": "price_growth"}]}],
        )
        synced_row = WidgetSubscription.objects.get(pk=sub.pk)
        mock_sync.return_value = (synced_row, None)

        out, err = downgrade_subscription(sub, "basic", business)
        assert err is None
        assert out is not None
        mock_sched_create.assert_called_once()
        mock_sched_modify.assert_called_once()

    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.create")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_downgrade_advanced_to_basic(self, mock_sub_ret, mock_sched_create, mock_sched_modify, mock_sync, business):
        business.stripe_customer_id = "cus_x"
        business.save(update_fields=["stripe_customer_id"])
        sub = WidgetSubscriptionFactory(
            business=business,
            plan_id="advanced",
            status="active",
            stripe_subscription_id="sub_dg2",
        )
        mock_sub_ret.return_value = make_service_subscription(
            sub_id="sub_dg2",
            current_price_id="price_advanced",
        )
        mock_sched_create.return_value = SimpleNamespace(
            id="sched_2",
            phases=[{"start_date": 1_700_000_000, "items": [{"price": "price_advanced"}]}],
        )
        mock_sync.return_value = (sub, None)
        _out, err = downgrade_subscription(sub, "basic", business)
        assert err is None

    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_downgrade_with_existing_schedule_updates_it(
        self, mock_sub_ret, mock_sched_ret, mock_sched_modify, mock_sync, business
    ):
        business.stripe_customer_id = "cus_x"
        business.save(update_fields=["stripe_customer_id"])
        sub = WidgetSubscriptionFactory(
            business=business,
            plan_id="growth",
            stripe_subscription_id="sub_ex",
        )
        sched = make_service_schedule(schedule_id="sched_ex")
        mock_sub_ret.return_value = make_service_subscription(
            sub_id="sub_ex",
            current_price_id="price_growth",
            schedule=sched,
        )
        mock_sched_ret.return_value = SimpleNamespace(
            id="sched_ex",
            phases=[{"start_date": 1_700_000_000, "items": [{"price": "price_growth"}]}],
        )
        mock_sync.return_value = (sub, None)
        _out, err = downgrade_subscription(sub, "basic", business)
        assert err is None
        mock_sched_modify.assert_called_once()
        mock_sched_ret.assert_called_once()

    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.release")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_downgrade_same_price_releases_schedule(
        self, mock_sub_ret, mock_sched_ret, mock_release, mock_sync, business
    ):
        business.stripe_customer_id = "cus_x"
        business.save(update_fields=["stripe_customer_id"])
        sub = WidgetSubscriptionFactory(
            business=business,
            plan_id="basic",
            stripe_subscription_id="sub_same",
        )
        sched = make_service_schedule(schedule_id="sched_rel")
        mock_sub_ret.return_value = make_service_subscription(
            sub_id="sub_same",
            current_price_id="price_basic",
            schedule=sched,
        )
        mock_sched_ret.return_value = SimpleNamespace(
            id="sched_rel",
            phases=[{"start_date": 1_700_000_000}],
        )
        mock_sync.return_value = (sub, None)
        _out, err = downgrade_subscription(sub, "basic", business)
        assert err is None
        mock_release.assert_called_once_with("sched_rel")

    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_downgrade_handles_stripe_error(self, mock_sub_ret, mock_sync, business):
        sub = WidgetSubscriptionFactory(
            business=business,
            stripe_subscription_id="sub_bad",
        )
        mock_sub_ret.side_effect = stripe.StripeError("retrieve failed")
        out, err = downgrade_subscription(sub, "basic", business)
        assert out is None
        assert "retrieve failed" in err


@pytest.mark.django_db
class TestCancelAtPeriodEnd:
    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_cancel_no_schedule_sets_cancel_at_period_end(self, mock_ret, mock_mod, mock_sync, business):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_c1")
        mock_ret.return_value = make_service_subscription(
            sub_id="sub_c1",
            schedule=None,
        )
        mock_sync.return_value = (sub, None)
        out, err = cancel_at_period_end(sub)
        assert err is None
        mock_mod.assert_called_once()
        assert mock_mod.call_args.kwargs.get("cancel_at_period_end") is True

    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_cancel_with_schedule_updates_schedule_end_behavior(
        self, mock_sub_ret, mock_sched_ret, mock_sched_mod, mock_sync, business
    ):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_c2")
        sched = make_service_schedule("sched_c")
        mock_sub_ret.return_value = make_service_subscription(
            sub_id="sub_c2",
            current_price_id="price_basic",
            schedule=sched,
        )
        mock_sched_ret.return_value = SimpleNamespace(
            id="sched_c",
            phases=[{"start_date": 1_700_000_000, "items": [{"price": "price_basic"}]}],
        )
        mock_sync.return_value = (sub, None)
        out, err = cancel_at_period_end(sub)
        assert err is None
        mock_sched_mod.assert_called_once()
        assert mock_sched_mod.call_args.kwargs.get("end_behavior") == "cancel"

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_cancel_handles_stripe_error(self, mock_ret, business):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_ce")
        mock_ret.side_effect = stripe.StripeError("nope")
        out, err = cancel_at_period_end(sub)
        assert out is None
        assert "nope" in err


@pytest.mark.django_db
class TestReactivate:
    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.modify")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_reactivate_no_schedule_clears_cancel(self, mock_ret, mock_mod, mock_sync, business):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_r1")
        mock_ret.return_value = make_service_subscription(sub_id="sub_r1", schedule=None)
        mock_sync.return_value = (sub, None)
        out, err = reactivate(sub)
        assert err is None
        mock_mod.assert_called_once()
        assert mock_mod.call_args.kwargs.get("cancel_at_period_end") is False

    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.SubscriptionSchedule.release")
    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_reactivate_with_schedule_releases_schedule(self, mock_ret, mock_release, mock_sync, business):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_r2")
        mock_ret.return_value = make_service_subscription(
            sub_id="sub_r2",
            schedule=make_service_schedule("sched_r"),
        )
        mock_sync.return_value = (sub, None)
        out, err = reactivate(sub)
        assert err is None
        mock_release.assert_called_once()

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    def test_reactivate_handles_stripe_error(self, mock_ret, business):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_re")
        mock_ret.side_effect = stripe.StripeError("fail")
        out, err = reactivate(sub)
        assert out is None
        assert "fail" in err


@pytest.mark.django_db
class TestSamePriceOpenInvoice:
    @patch("quickstart.services.widget_subscription_service.sync_widget_subscription_from_stripe")
    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    def test_same_price_no_open_invoice_returns_success(self, mock_list, mock_sync, business):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_sp")
        mock_list.return_value = make_stripe_list([])
        mock_sync.return_value = (sub, None)
        req, secret, row = same_price_open_invoice(sub, "basic", "price_basic")
        assert req is False
        assert secret is None
        assert row == sub

    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    @patch("quickstart.services.widget_subscription_service.stripe.PaymentIntent.retrieve")
    def test_same_price_open_invoice_returns_client_secret(self, mock_pi, mock_list, business):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_sp2")
        inv = make_stripe_invoice(
            payment_intent="pi_open",
        )
        mock_list.return_value = make_stripe_list([inv])
        mock_pi.return_value = make_stripe_payment_intent(status="requires_confirmation")
        req, secret, row = same_price_open_invoice(sub, "basic", "price_basic")
        assert req is True
        assert secret == "pi_test_secret"
        assert row == sub

    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    @patch("quickstart.services.widget_subscription_service.stripe.PaymentIntent.retrieve")
    def test_same_price_open_invoice_no_client_secret_returns_none(self, mock_pi, mock_list, business):
        sub = WidgetSubscriptionFactory(business=business, stripe_subscription_id="sub_sp3")
        inv = make_stripe_invoice(payment_intent="pi_x")
        mock_list.return_value = make_stripe_list([inv])
        mock_pi.return_value = make_stripe_payment_intent(
            status="succeeded",
            client_secret=None,
        )
        req, secret, row = same_price_open_invoice(sub, "basic", "price_basic")
        assert req is None
        assert secret is None


@pytest.mark.django_db
class TestProrationAfterSubscriptionItemModify:
    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    @patch("quickstart.services.widget_subscription_service.stripe.PaymentIntent.retrieve")
    def test_proration_returns_payment_when_client_secret_available(self, mock_pi, mock_list):
        inv = make_stripe_invoice(payment_intent="pi_pr")
        stripe_sub = SimpleNamespace(
            id="sub_p",
            latest_invoice=inv,
        )
        mock_pi.return_value = make_stripe_payment_intent()
        mock_list.return_value = make_stripe_list([])
        outcome, secret, err, verified = proration_after_subscription_item_modify(
            stripe_sub, "sub_p", "price_growth"
        )
        assert outcome == "payment"
        assert secret == "pi_test_secret"
        assert err is None

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    def test_proration_returns_success_when_price_matches_and_no_open_invoice(
        self, mock_list, mock_sub_ret
    ):
        stripe_sub = SimpleNamespace(
            id="sub_ok",
            latest_invoice=make_stripe_invoice(status="paid"),
        )
        mock_list.return_value = make_stripe_list([])
        verified = make_stripe_subscription(
            sub_id="sub_ok",
            status="active",
            price_id="price_growth",
        )
        mock_sub_ret.return_value = verified
        outcome, secret, err, out_sub = proration_after_subscription_item_modify(
            stripe_sub, "sub_ok", "price_growth"
        )
        assert outcome == "success"
        assert secret is None
        assert err is None
        assert out_sub == verified

    @patch("quickstart.services.widget_subscription_service.stripe.Subscription.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    def test_proration_returns_error_when_price_mismatch(self, mock_list, mock_sub_ret):
        stripe_sub = SimpleNamespace(
            id="sub_bad",
            latest_invoice=make_stripe_invoice(status="paid"),
        )
        mock_list.return_value = make_stripe_list([])
        mock_sub_ret.return_value = make_stripe_subscription(
            sub_id="sub_bad",
            price_id="price_basic",
        )
        outcome, secret, err, verified = proration_after_subscription_item_modify(
            stripe_sub, "sub_bad", "price_growth"
        )
        assert outcome == "error"
        assert err is not None

    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    def test_proration_returns_error_when_open_invoice_exists_but_no_secret(self, mock_list):
        inv = make_stripe_invoice(status="open", payment_intent=None)
        stripe_sub = SimpleNamespace(
            id="sub_oi",
            latest_invoice=inv,
        )
        mock_list.return_value = make_stripe_list([])
        outcome, secret, err, _ = proration_after_subscription_item_modify(
            stripe_sub, "sub_oi", "price_growth"
        )
        assert outcome == "error"
        assert "no longer valid" in err.lower()

    @patch("quickstart.services.widget_subscription_service.stripe.PaymentIntent.retrieve")
    @patch("quickstart.services.widget_subscription_service.stripe.Invoice.list")
    def test_proration_checks_open_invoices_as_fallback(self, mock_list, mock_pi):
        """Open subscription invoice yields client_secret (first Invoice.list block)."""
        stripe_sub = SimpleNamespace(
            id="sub_fb",
            latest_invoice=make_stripe_invoice(status="paid"),
        )
        mock_list.return_value = make_stripe_list(
            [make_stripe_invoice(payment_intent="pi_fb", status="open")]
        )
        mock_pi.return_value = make_stripe_payment_intent()
        outcome, secret, err, _ = proration_after_subscription_item_modify(
            stripe_sub, "sub_fb", "price_growth"
        )
        assert outcome == "payment"
        assert secret == "pi_test_secret"
