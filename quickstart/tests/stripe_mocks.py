"""
Reusable dict-shaped objects for mocking Stripe API responses in tests.
Code paths use isinstance(x, dict) with .get() or getattr on objects — dicts cover the dict branches.

`make_service_subscription` uses SimpleNamespace because widget_subscription_service uses
getattr() on subscription line items (e.g. current_period_end), which plain dicts do not support.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional


def make_stripe_payment_intent(
    pi_id: str = "pi_test_123",
    status: str = "requires_confirmation",
    client_secret: str = "pi_test_secret",
) -> dict:
    return {
        "id": pi_id,
        "status": status,
        "client_secret": client_secret,
    }


def make_stripe_invoice(
    inv_id: str = "in_test_123",
    status: str = "open",
    payment_intent: Any = None,
    payments: Optional[dict] = None,
) -> dict:
    inv: dict = {
        "id": inv_id,
        "status": status,
    }
    if payment_intent is not None:
        inv["payment_intent"] = payment_intent
    if payments is not None:
        inv["payments"] = payments
    return inv


def make_stripe_subscription_item(
    item_id: str = "si_test_item",
    price_id: str = "price_basic",
    current_period_end: Optional[int] = None,
) -> dict:
    item: dict = {
        "id": item_id,
        "price": {"id": price_id},
    }
    if current_period_end is not None:
        item["current_period_end"] = current_period_end
    return item


def make_stripe_subscription(
    sub_id: str = "sub_test_123",
    status: str = "active",
    price_id: str = "price_basic",
    customer_id: str = "cus_test_123",
    business_id: str = "1",
    plan_id: str = "basic",
    *,
    items: Optional[List[dict]] = None,
    latest_invoice: Any = None,
    current_period_end: Optional[int] = None,
    cancel_at_period_end: bool = False,
    schedule: Any = None,
    metadata_extra: Optional[dict] = None,
) -> dict:
    meta = {"business_id": business_id, "plan_id": plan_id}
    if metadata_extra:
        meta.update(metadata_extra)
    item_list = items or [
        make_stripe_subscription_item(price_id=price_id, current_period_end=current_period_end)
    ]
    sub: dict = {
        "id": sub_id,
        "status": status,
        "customer": customer_id,
        "metadata": meta,
        "items": {"data": item_list},
        "cancel_at_period_end": cancel_at_period_end,
    }
    if latest_invoice is not None:
        sub["latest_invoice"] = latest_invoice
    if current_period_end is not None and "current_period_end" not in sub:
        sub["current_period_end"] = current_period_end
    if schedule is not None:
        sub["schedule"] = schedule
    return sub


def make_stripe_schedule_phase(
    *,
    start_date: int,
    end_date: Optional[int] = None,
    items: Optional[List[dict]] = None,
) -> dict:
    phase_items = items or [{"price": "price_basic"}]
    phase: dict = {"start_date": start_date, "items": phase_items}
    if end_date is not None:
        phase["end_date"] = end_date
    return phase


def make_stripe_schedule(
    schedule_id: str = "sub_sched_test",
    phases: Optional[List[dict]] = None,
    end_behavior: Optional[str] = None,
) -> dict:
    sch: dict = {"id": schedule_id, "phases": phases or []}
    if end_behavior is not None:
        sch["end_behavior"] = end_behavior
    return sch


def make_stripe_customer(
    customer_id: str = "cus_test_123",
    *,
    default_payment_method: Any = None,
) -> dict:
    inv_settings = {}
    if default_payment_method is not None:
        inv_settings["default_payment_method"] = default_payment_method
    return {
        "id": customer_id,
        "invoice_settings": inv_settings,
    }


def make_stripe_list(data: List[Any]) -> SimpleNamespace:
    """Stripe SDK list objects expose .data; plain dicts do not (getattr(dict, 'data', None) is None)."""
    return SimpleNamespace(data=data)


def make_service_subscription(
    *,
    sub_id: str = "sub_test_123",
    status: str = "active",
    current_price_id: str = "price_basic",
    item_id: str = "si_test_item",
    period_end_ts: int = 2_000_000_000,
    schedule: Any = None,
    cancel_at_period_end: bool = False,
    customer_id: str = "cus_test_123",
    latest_invoice: Any = None,
    business_id: str = "1",
    plan_id_meta: str = "basic",
) -> SimpleNamespace:
    """Stripe-like subscription for widget_subscription_service (attribute access on items)."""
    item = SimpleNamespace(
        id=item_id,
        price=SimpleNamespace(id=current_price_id),
        current_period_end=period_end_ts,
    )
    return SimpleNamespace(
        id=sub_id,
        status=status,
        customer=customer_id,
        items=SimpleNamespace(data=[item]),
        schedule=schedule,
        cancel_at_period_end=cancel_at_period_end,
        current_period_end=period_end_ts,
        latest_invoice=latest_invoice,
        metadata=SimpleNamespace(business_id=business_id, plan_id=plan_id_meta),
    )


def make_service_schedule(
    schedule_id: str = "sub_sched_test",
    phases: Optional[List[Any]] = None,
    end_behavior: Optional[str] = None,
) -> SimpleNamespace:
    sch = SimpleNamespace(id=schedule_id, phases=phases or [])
    if end_behavior is not None:
        sch.end_behavior = end_behavior
    return sch
