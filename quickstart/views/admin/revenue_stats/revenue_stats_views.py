# quickstart/views/admin/revenue_stats/revenue_stats_views.py
from __future__ import annotations

import csv
import logging
import stripe
from functools import lru_cache
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from io import StringIO

from django.conf import settings
from django.db.models import (
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    Q,
    Sum,
    Value,
)
from django.db.models.functions import Cast, Coalesce, TruncDate, TruncMonth, TruncWeek
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from quickstart.models import (
    ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
    BusinessAddonSubscription,
    BusinessInfo,
    CorporateBooking,
    MembershipPayment,
    Payment,
    WidgetSubscription,
)
from quickstart.utils.permissions import CanViewPlatformRevenue
from quickstart.utils.stripe_processing_fee import estimate_stripe_processing_fee
from quickstart.utils.widget_booking_source import WIDGET_BOOKING_SOURCES

logger = logging.getLogger(__name__)

HST_RATE = Decimal("0.13")
WIDGET_SOURCE_LIST = list(WIDGET_BOOKING_SOURCES)

SOURCE_MARKETPLACE = "marketplace"
SOURCE_WIDGET = "widget"
SOURCE_CORPORATE = "corporate"
SOURCE_MEMBERSHIP = "membership"
SOURCE_SAAS = "saas"
SOURCE_ADDON = "addon"

ALL_SOURCES = [
    SOURCE_MARKETPLACE,
    SOURCE_WIDGET,
    SOURCE_CORPORATE,
    SOURCE_MEMBERSHIP,
    SOURCE_SAAS,
    SOURCE_ADDON,
]

DEC_FIELD = DecimalField(max_digits=18, decimal_places=4)


def _f(x: Decimal | None) -> float:
    if x is None:
        return 0.0
    return float(Decimal(x).quantize(Decimal("0.01")))


def _parse_sources(raw: str | None) -> set[str]:
    if not raw:
        return set(ALL_SOURCES)
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    allowed = set(ALL_SOURCES)
    selected = parts & allowed
    return selected if selected else set(ALL_SOURCES)


def _parse_granularity(raw: str | None) -> str:
    g = (raw or "month").strip().lower()
    if g not in {"day", "week", "month"}:
        return "month"
    return g


def _trunc_cls(granularity: str):
    if granularity == "day":
        return TruncDate
    if granularity == "week":
        return TruncWeek
    return TruncMonth


def _daterange_params(start_date_str: str | None, end_date_str: str | None):
    today = timezone.now().date()
    if start_date_str and end_date_str:
        try:
            start_d = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_d = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError as e:
            raise ValueError("Dates must be YYYY-MM-DD") from e
    else:
        end_d = today
        start_d = end_d - timedelta(days=89)
    if start_d > end_d:
        raise ValueError("start_date must be on or before end_date")
    return start_d, end_d


def _to_dt_bounds(start_d: date, end_d: date):
    start_dt = datetime.combine(
        start_d, datetime.min.time(), tzinfo=datetime_timezone.utc
    )
    end_dt = datetime.combine(
        end_d, datetime.max.time(), tzinfo=datetime_timezone.utc
    )
    return start_dt, end_dt


def _previous_period_bounds(start_d: date, end_d: date):
    days = (end_d - start_d).days + 1
    prev_end = start_d - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return prev_start, prev_end


def _corp_fee_pct_decimal() -> Decimal:
    return Decimal(str(getattr(settings, "CORPORATE_PLATFORM_FEE_PERCENT", "13"))) / Decimal(
        "100"
    )


def _payment_base_qs(start_dt, end_dt):
    return Payment.objects.filter(
        status="succeeded",
        created_at__gte=start_dt,
        created_at__lte=end_dt,
    )


def _merge_money(dst: dict, src: dict):
    for k in (
        "commission",
        "commission_plus_tax",
        "stripe_processing",
        "gross_gmv",
        "net_after_stripe",
    ):
        dst[k] = dst.get(k, Decimal("0")) + src.get(k, Decimal("0"))
    dst["cnt"] = dst.get("cnt", 0) + src.get("cnt", 0)


def _empty_money():
    return {
        "commission": Decimal("0"),
        "commission_plus_tax": Decimal("0"),
        "stripe_processing": Decimal("0"),
        "gross_gmv": Decimal("0"),
        "net_after_stripe": Decimal("0"),
        "cnt": 0,
    }


def _payment_aggregate(qs):
    agg = qs.aggregate(
        commission=Coalesce(Sum("platform_fee_amount"), Decimal("0")),
        commission_plus_tax=Coalesce(
            Sum(F("platform_fee_amount") + F("platform_fee_tax")), Decimal("0")
        ),
        stripe_processing=Coalesce(Sum("stripe_processing_fee"), Decimal("0")),
        gross_gmv=Coalesce(Sum("amount"), Decimal("0")),
        cnt=Count("id"),
    )
    agg["net_after_stripe"] = agg["commission"] - agg["stripe_processing"]
    return agg


def _membership_aggregate(qs):
    stripe_est = ExpressionWrapper(
        F("amount") * Value(Decimal("0.029")) + Value(Decimal("0.30")),
        output_field=DEC_FIELD,
    )
    agg = qs.aggregate(
        commission=Coalesce(Sum("platform_fee_amount"), Decimal("0")),
        stripe_processing=Coalesce(Sum(stripe_est), Decimal("0")),
        gross_gmv=Coalesce(Sum("amount"), Decimal("0")),
        cnt=Count("id"),
    )
    agg["commission_plus_tax"] = agg["commission"]
    agg["net_after_stripe"] = agg["commission"] - agg["stripe_processing"]
    return agg


def _membership_base_qs(start_dt, end_dt):
    return MembershipPayment.objects.filter(
        status="paid",
        created_at__gte=start_dt,
        created_at__lte=end_dt,
    )


def _deposit_corporate_exprs():
    fee = _corp_fee_pct_decimal()
    gross_usd = ExpressionWrapper(
        Cast(F("deposit_cents"), DEC_FIELD) / Value(Decimal("100")),
        output_field=DEC_FIELD,
    )
    commission = ExpressionWrapper(
        gross_usd * Value(fee),
        output_field=DEC_FIELD,
    )
    tax_part = ExpressionWrapper(
        commission * Value(HST_RATE),
        output_field=DEC_FIELD,
    )
    commission_plus_tax = ExpressionWrapper(
        commission + tax_part,
        output_field=DEC_FIELD,
    )
    stripe_est = ExpressionWrapper(
        gross_usd * Value(Decimal("0.029")) + Value(Decimal("0.30")),
        output_field=DEC_FIELD,
    )
    net_after = ExpressionWrapper(
        commission - stripe_est,
        output_field=DEC_FIELD,
    )
    return gross_usd, commission, commission_plus_tax, stripe_est, net_after


def _balance_corporate_exprs():
    fee = _corp_fee_pct_decimal()
    gross_usd = ExpressionWrapper(
        Cast(F("balance_cents"), DEC_FIELD) / Value(Decimal("100")),
        output_field=DEC_FIELD,
    )
    commission = ExpressionWrapper(
        gross_usd * Value(fee),
        output_field=DEC_FIELD,
    )
    tax_part = ExpressionWrapper(
        commission * Value(HST_RATE),
        output_field=DEC_FIELD,
    )
    commission_plus_tax = ExpressionWrapper(
        commission + tax_part,
        output_field=DEC_FIELD,
    )
    stripe_est = ExpressionWrapper(
        gross_usd * Value(Decimal("0.029")) + Value(Decimal("0.30")),
        output_field=DEC_FIELD,
    )
    net_after = ExpressionWrapper(
        commission - stripe_est,
        output_field=DEC_FIELD,
    )
    return gross_usd, commission, commission_plus_tax, stripe_est, net_after


def _corporate_aggregate_window(start_dt, end_dt):
    gross_usd_d, comm_d, cpt_d, stripe_d, net_d = _deposit_corporate_exprs()
    gross_usd_b, comm_b, cpt_b, stripe_b, net_b = _balance_corporate_exprs()

    dep = CorporateBooking.objects.filter(
        deposit_paid_at__gte=start_dt,
        deposit_paid_at__lte=end_dt,
    ).aggregate(
        commission=Coalesce(Sum(comm_d), Decimal("0")),
        commission_plus_tax=Coalesce(Sum(cpt_d), Decimal("0")),
        stripe_processing=Coalesce(Sum(stripe_d), Decimal("0")),
        gross_gmv=Coalesce(Sum(gross_usd_d), Decimal("0")),
        cnt=Count("id"),
        net_after_stripe=Coalesce(Sum(net_d), Decimal("0")),
    )

    bal = CorporateBooking.objects.filter(
        balance_paid_at__gte=start_dt,
        balance_paid_at__lte=end_dt,
    ).aggregate(
        commission=Coalesce(Sum(comm_b), Decimal("0")),
        commission_plus_tax=Coalesce(Sum(cpt_b), Decimal("0")),
        stripe_processing=Coalesce(Sum(stripe_b), Decimal("0")),
        gross_gmv=Coalesce(Sum(gross_usd_b), Decimal("0")),
        cnt=Count("id"),
        net_after_stripe=Coalesce(Sum(net_b), Decimal("0")),
    )

    keys = (
        "commission",
        "commission_plus_tax",
        "stripe_processing",
        "gross_gmv",
        "cnt",
        "net_after_stripe",
    )
    out = {}
    for k in keys:
        out[k] = dep[k] + bal[k]
    return out


def _subscription_end_date(sub, horizon_end: date) -> date:
    if getattr(sub, "status", None) in ("canceled", "incomplete_expired"):
        end_ts = getattr(sub, "updated_at", None) or timezone.now()
        return min(end_ts.date(), horizon_end)
    cpe = getattr(sub, "current_period_end", None)
    if cpe:
        return min(cpe.date(), horizon_end)
    return horizon_end


def _spread_monthly_across_calendar(
    monthly_cad: Decimal, start_d: date, end_d: date
) -> Decimal:
    if monthly_cad <= 0 or start_d > end_d:
        return Decimal("0")
    total = Decimal("0")
    y, m = start_d.year, start_d.month
    while True:
        dim = monthrange(y, m)[1]
        month_first = date(y, m, 1)
        month_last = date(y, m, dim)
        ov_start = max(start_d, month_first)
        ov_end = min(end_d, month_last)
        if ov_start <= ov_end:
            days = (ov_end - ov_start).days + 1
            total += monthly_cad * Decimal(days) / Decimal(dim)
        if month_last >= end_d:
            break
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return total.quantize(Decimal("0.01"))


def _stripe_money_from_price(price) -> Decimal | None:
    """Stripe Price unit amount -> Decimal dollars."""
    ud = getattr(price, "unit_amount_decimal", None)
    if ud not in (None, ""):
        try:
            return Decimal(str(ud)).quantize(Decimal("0.01"))
        except Exception:
            pass
    ua = getattr(price, "unit_amount", None)
    if ua is None:
        return None
    return (Decimal(int(ua)) / Decimal("100")).quantize(Decimal("0.01"))


def _stripe_price_monthly_amount_from_price(price) -> Decimal | None:
    rec = getattr(price, "recurring", None)
    if not rec:
        return None
    interval = (getattr(rec, "interval", None) or "").lower()
    ic_raw = getattr(rec, "interval_count", None) or 1
    try:
        ic = Decimal(str(ic_raw))
    except Exception:
        ic = Decimal("1")
    if ic <= 0:
        ic = Decimal("1")

    unit = _stripe_money_from_price(price)
    if unit is None or unit <= 0:
        return None

    if interval == "month":
        return (unit / ic).quantize(Decimal("0.01"))
    if interval == "year":
        return (unit / ic / Decimal("12")).quantize(Decimal("0.01"))
    if interval == "week":
        weeks_per_month = Decimal("365") / Decimal("12") / Decimal("7")
        return (unit / ic * weeks_per_month).quantize(Decimal("0.01"))
    if interval == "day":
        days_per_month = Decimal("365") / Decimal("12")
        return (unit / ic * days_per_month).quantize(Decimal("0.01"))
    return None


@lru_cache(maxsize=512)
def _stripe_price_monthly_amount(price_id: str) -> Decimal | None:
    pid = (price_id or "").strip()
    if not pid:
        return None
    secret = getattr(settings, "STRIPE_SECRET_KEY", None)
    if not secret:
        logger.warning("Missing STRIPE_SECRET_KEY; cannot resolve Stripe Price %s", pid)
        return None
    try:
        stripe.api_key = secret
        price = stripe.Price.retrieve(pid)
        return _stripe_price_monthly_amount_from_price(price)
    except Exception as e:
        logger.warning("Stripe Price.retrieve failed for %s: %s", pid, e)
        return None


def _widget_plan_fallback_price_id(plan_id: str | None) -> str | None:
    pid = (plan_id or "basic").strip().lower()
    mapping = {
        "basic": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC", None) or "",
        "growth": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH", None) or "",
        "advanced": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED", None) or "",
    }
    raw = (mapping.get(pid) or "").strip() or (
        getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ID", None) or ""
    ).strip()
    return raw or None


def _widget_subscription_monthly_amount(sub: WidgetSubscription) -> Decimal | None:
    pid = (sub.stripe_price_id or "").strip() or _widget_plan_fallback_price_id(sub.plan_id)
    return _stripe_price_monthly_amount(pid) if pid else None


def _addon_subscription_monthly_amount(sub: BusinessAddonSubscription) -> Decimal | None:
    pid = (sub.stripe_price_id or "").strip() or (
        getattr(settings, "MARKETPLACE_EMAIL_ADDON_PRICE_ID", None) or ""
    ).strip()
    return _stripe_price_monthly_amount(pid) if pid else None


def _saas_accrual_aggregate(start_d: date, end_d: date) -> dict:
    commission = Decimal("0")
    stripe_total = Decimal("0")
    gross = Decimal("0")
    contrib = 0
    qs = WidgetSubscription.objects.filter(
        status__in=("active", "trialing", "past_due"),
    )
    for sub in qs.iterator(chunk_size=200):
        monthly = _widget_subscription_monthly_amount(sub)
        if monthly is None or monthly <= 0:
            continue
        span_start = max(start_d, sub.created_at.date())
        span_end = min(end_d, _subscription_end_date(sub, end_d))
        if span_start > span_end:
            continue
        piece = _spread_monthly_across_calendar(monthly, span_start, span_end)
        if piece <= 0:
            continue
        commission += piece
        gross += piece
        stripe_total += estimate_stripe_processing_fee(piece)
        contrib += 1

    commission = commission.quantize(Decimal("0.01"))
    stripe_total = stripe_total.quantize(Decimal("0.01"))
    gross = gross.quantize(Decimal("0.01"))
    net_after = (commission - stripe_total).quantize(Decimal("0.01"))
    return {
        "commission": commission,
        "commission_plus_tax": commission,
        "stripe_processing": stripe_total,
        "gross_gmv": gross,
        "cnt": contrib,
        "net_after_stripe": net_after,
    }


def _addon_accrual_aggregate(start_d: date, end_d: date) -> dict:
    commission = Decimal("0")
    stripe_total = Decimal("0")
    gross = Decimal("0")
    contrib = 0
    qs = BusinessAddonSubscription.objects.filter(
        addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
        status__in=("active", "trialing", "past_due"),
    )
    for sub in qs.iterator(chunk_size=200):
        monthly = _addon_subscription_monthly_amount(sub)
        if monthly is None or monthly <= 0:
            continue
        span_start = max(start_d, sub.created_at.date())
        span_end = min(end_d, _subscription_end_date(sub, end_d))
        if span_start > span_end:
            continue
        piece = _spread_monthly_across_calendar(monthly, span_start, span_end)
        if piece <= 0:
            continue
        commission += piece
        gross += piece
        stripe_total += estimate_stripe_processing_fee(piece)
        contrib += 1

    commission = commission.quantize(Decimal("0.01"))
    stripe_total = stripe_total.quantize(Decimal("0.01"))
    gross = gross.quantize(Decimal("0.01"))
    net_after = (commission - stripe_total).quantize(Decimal("0.01"))
    return {
        "commission": commission,
        "commission_plus_tax": commission,
        "stripe_processing": stripe_total,
        "gross_gmv": gross,
        "cnt": contrib,
        "net_after_stripe": net_after,
    }


def _pct_delta(prev: Decimal, curr: Decimal) -> float | None:
    if prev == 0:
        return 0.0 if curr == 0 else None
    return float(((curr - prev) / prev) * Decimal("100"))


def _serialize_money_block(m: dict) -> dict:
    return {
        "commission": _f(m.get("commission")),
        "commission_plus_tax": _f(m.get("commission_plus_tax")),
        "net_after_stripe": _f(m.get("net_after_stripe")),
        "gross_gmv": _f(m.get("gross_gmv")),
        "transactions": int(m.get("cnt") or 0),
        "stripe_processing_estimate": _f(m.get("stripe_processing")),
    }


def _refund_volume(start_dt, end_dt) -> Decimal:
    return Payment.objects.filter(
        refund_date__gte=start_dt,
        refund_date__lte=end_dt,
    ).aggregate(t=Coalesce(Sum("refunded_amount"), Decimal("0")))["t"]


def _booking_take_rate(marketplace_qs, widget_qs) -> float | None:
    agg_m = marketplace_qs.aggregate(
        c=Coalesce(Sum("platform_fee_amount"), Decimal("0")),
        g=Coalesce(Sum("amount"), Decimal("0")),
    )
    agg_w = widget_qs.aggregate(
        c=Coalesce(Sum("platform_fee_amount"), Decimal("0")),
        g=Coalesce(Sum("amount"), Decimal("0")),
    )
    c = agg_m["c"] + agg_w["c"]
    g = agg_m["g"] + agg_w["g"]
    if g <= 0:
        return None
    return float((c / g) * Decimal("100"))


def _trunc_bucket_to_iso_day(bucket) -> str:
    """
    TruncDate may return datetime or date depending on DB backend.
    Normalize to YYYY-MM-DD for grouping.
    """
    if bucket is None:
        return ""
    if isinstance(bucket, datetime):
        return bucket.date().isoformat()
    if isinstance(bucket, date):
        return bucket.isoformat()
    return str(bucket)


def _daily_payment_commissions(start_dt, end_dt, marketplace_qs, widget_qs):
    trunc = TruncDate("created_at")
    day_m = {}
    for row in (
        marketplace_qs.annotate(bucket=trunc)
        .values("bucket")
        .annotate(c=Coalesce(Sum("platform_fee_amount"), Decimal("0")))
    ):
        if row["bucket"]:
            day_m[_trunc_bucket_to_iso_day(row["bucket"])] = row["c"]

    day_w = {}
    for row in (
        widget_qs.annotate(bucket=trunc)
        .values("bucket")
        .annotate(c=Coalesce(Sum("platform_fee_amount"), Decimal("0")))
    ):
        if row["bucket"]:
            day_w[_trunc_bucket_to_iso_day(row["bucket"])] = row["c"]

    day_mem = {}
    for row in (
        _membership_base_qs(start_dt, end_dt)
        .annotate(bucket=trunc)
        .values("bucket")
        .annotate(c=Coalesce(Sum("platform_fee_amount"), Decimal("0")))
    ):
        if row["bucket"]:
            day_mem[_trunc_bucket_to_iso_day(row["bucket"])] = row["c"]

    keys = sorted(set(day_m) | set(day_w) | set(day_mem))
    series = []
    for k in keys:
        series.append(
            {
                "day": k,
                "commission": _f(day_m.get(k, Decimal("0")) + day_w.get(k, Decimal("0")) + day_mem.get(k, Decimal("0"))),
            }
        )
    return series


def _rollup_daily_to_buckets(daily_series: list[dict], granularity: str, start_d: date, end_d: date):
    """Roll iso-day commissions into week/month buckets aligned to trunc semantics."""
    if not daily_series:
        return []

    by_day = {row["day"]: Decimal(str(row["commission"])) for row in daily_series}

    def bucket_for(d: date) -> str:
        if granularity == "day":
            return d.isoformat()
        if granularity == "week":
            year, week, _ = d.isocalendar()
            return f"{year}-W{week:02d}"
        return f"{d.year}-{d.month:02d}"

    buckets = defaultdict(lambda: Decimal("0"))
    cur = start_d
    while cur <= end_d:
        buckets[bucket_for(cur)] += by_day.get(cur.isoformat(), Decimal("0"))
        cur += timedelta(days=1)

    out = []
    for k in sorted(buckets.keys()):
        out.append({"bucket": k, "commission": _f(buckets[k])})
    return out


def _sources_totals(start_dt, end_dt, sources: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    base = _payment_base_qs(start_dt, end_dt)
    marketplace_qs = base.exclude(
        metadata__original_stripe_metadata__booking_source__in=WIDGET_SOURCE_LIST
    )
    widget_qs = base.filter(
        metadata__original_stripe_metadata__booking_source__in=WIDGET_SOURCE_LIST
    )

    if SOURCE_MARKETPLACE in sources:
        out[SOURCE_MARKETPLACE] = _payment_aggregate(marketplace_qs)
        out[SOURCE_MARKETPLACE]["currency"] = "CAD"

    if SOURCE_WIDGET in sources:
        out[SOURCE_WIDGET] = _payment_aggregate(widget_qs)
        out[SOURCE_WIDGET]["currency"] = "CAD"

    if SOURCE_MEMBERSHIP in sources:
        mem = _membership_aggregate(_membership_base_qs(start_dt, end_dt))
        mem["currency"] = "CAD"
        out[SOURCE_MEMBERSHIP] = mem

    if SOURCE_CORPORATE in sources:
        corp = _corporate_aggregate_window(start_dt, end_dt)
        corp["currency"] = "CAD"
        out[SOURCE_CORPORATE] = corp

    if SOURCE_SAAS in sources:
        saas = _saas_accrual_aggregate(start_dt.date(), end_dt.date())
        saas["currency"] = "CAD"
        out[SOURCE_SAAS] = saas

    if SOURCE_ADDON in sources:
        addon = _addon_accrual_aggregate(start_dt.date(), end_dt.date())
        addon["currency"] = "CAD"
        out[SOURCE_ADDON] = addon

    return out


def _cad_totals(sources_totals: dict[str, dict]) -> dict:
    """Roll up all selected revenue streams (labeled CAD for reporting)."""
    merged = _empty_money()
    for row in sources_totals.values():
        _merge_money(merged, row)
    return merged


def _series_payment_buckets(qs, trunc, field_name: str):
    rows = (
        qs.annotate(bucket=trunc(field_name))
        .values("bucket")
        .annotate(
            commission=Coalesce(Sum("platform_fee_amount"), Decimal("0")),
            commission_plus_tax=Coalesce(
                Sum(F("platform_fee_amount") + F("platform_fee_tax")), Decimal("0")
            ),
            stripe_processing=Coalesce(Sum("stripe_processing_fee"), Decimal("0")),
            gross_gmv=Coalesce(Sum("amount"), Decimal("0")),
            cnt=Count("id"),
        )
        .order_by("bucket")
    )
    series = []
    for row in rows:
        if not row["bucket"]:
            continue
        bucket_key = row["bucket"].isoformat() if hasattr(row["bucket"], "isoformat") else str(row["bucket"])
        commission = row["commission"]
        stripe_processing = row["stripe_processing"]
        series.append(
            {
                "bucket": bucket_key,
                "commission": commission,
                "commission_plus_tax": row["commission_plus_tax"],
                "stripe_processing": stripe_processing,
                "net_after_stripe": commission - stripe_processing,
                "gross_gmv": row["gross_gmv"],
                "cnt": row["cnt"],
            }
        )
    return series


def _series_membership_buckets(start_dt, end_dt, trunc):
    qs = _membership_base_qs(start_dt, end_dt)
    stripe_est = ExpressionWrapper(
        F("amount") * Value(Decimal("0.029")) + Value(Decimal("0.30")),
        output_field=DEC_FIELD,
    )
    rows = (
        qs.annotate(bucket=trunc("created_at"))
        .values("bucket")
        .annotate(
            commission=Coalesce(Sum("platform_fee_amount"), Decimal("0")),
            stripe_processing=Coalesce(Sum(stripe_est), Decimal("0")),
            gross_gmv=Coalesce(Sum("amount"), Decimal("0")),
            cnt=Count("id"),
        )
        .order_by("bucket")
    )
    series = []
    for row in rows:
        if not row["bucket"]:
            continue
        bucket_key = row["bucket"].isoformat() if hasattr(row["bucket"], "isoformat") else str(row["bucket"])
        commission = row["commission"]
        stripe_processing = row["stripe_processing"]
        series.append(
            {
                "bucket": bucket_key,
                "commission": commission,
                "commission_plus_tax": commission,
                "stripe_processing": stripe_processing,
                "net_after_stripe": commission - stripe_processing,
                "gross_gmv": row["gross_gmv"],
                "cnt": row["cnt"],
            }
        )
    return series


def _series_corporate_buckets(start_dt, end_dt, trunc):
    gross_usd_d, comm_d, cpt_d, stripe_d, net_d = _deposit_corporate_exprs()
    gross_usd_b, comm_b, cpt_b, stripe_b, net_b = _balance_corporate_exprs()

    dep_rows = (
        CorporateBooking.objects.filter(
            deposit_paid_at__gte=start_dt,
            deposit_paid_at__lte=end_dt,
        )
        .annotate(bucket=trunc("deposit_paid_at"))
        .values("bucket")
        .annotate(
            commission=Coalesce(Sum(comm_d), Decimal("0")),
            commission_plus_tax=Coalesce(Sum(cpt_d), Decimal("0")),
            stripe_processing=Coalesce(Sum(stripe_d), Decimal("0")),
            gross_gmv=Coalesce(Sum(gross_usd_d), Decimal("0")),
            cnt=Count("id"),
            net_after_stripe=Coalesce(Sum(net_d), Decimal("0")),
        )
    )

    bal_rows = (
        CorporateBooking.objects.filter(
            balance_paid_at__gte=start_dt,
            balance_paid_at__lte=end_dt,
        )
        .annotate(bucket=trunc("balance_paid_at"))
        .values("bucket")
        .annotate(
            commission=Coalesce(Sum(comm_b), Decimal("0")),
            commission_plus_tax=Coalesce(Sum(cpt_b), Decimal("0")),
            stripe_processing=Coalesce(Sum(stripe_b), Decimal("0")),
            gross_gmv=Coalesce(Sum(gross_usd_b), Decimal("0")),
            cnt=Count("id"),
            net_after_stripe=Coalesce(Sum(net_b), Decimal("0")),
        )
    )

    merged = defaultdict(
        lambda: {
            "commission": Decimal("0"),
            "commission_plus_tax": Decimal("0"),
            "stripe_processing": Decimal("0"),
            "gross_gmv": Decimal("0"),
            "cnt": 0,
            "net_after_stripe": Decimal("0"),
        }
    )

    def ingest(rows):
        for row in rows:
            if not row["bucket"]:
                continue
            bucket_key = (
                row["bucket"].isoformat()
                if hasattr(row["bucket"], "isoformat")
                else str(row["bucket"])
            )
            tgt = merged[bucket_key]
            tgt["commission"] += row["commission"]
            tgt["commission_plus_tax"] += row["commission_plus_tax"]
            tgt["stripe_processing"] += row["stripe_processing"]
            tgt["gross_gmv"] += row["gross_gmv"]
            tgt["cnt"] += row["cnt"]
            tgt["net_after_stripe"] += row["net_after_stripe"]

    ingest(dep_rows)
    ingest(bal_rows)

    series = []
    for bucket_key in sorted(merged.keys()):
        row = merged[bucket_key]
        series.append({"bucket": bucket_key, **row})
    return series


def _merge_timeseries(by_source: dict[str, list[dict]]) -> list[dict]:
    """Flatten into rows with source field."""
    out = []
    for source, rows in by_source.items():
        for row in rows:
            out.append(
                {
                    "bucket": row["bucket"],
                    "source": source,
                    "commission": _f(row["commission"]),
                    "commission_plus_tax": _f(row["commission_plus_tax"]),
                    "net_after_stripe": _f(row["net_after_stripe"]),
                    "gross_gmv": _f(row["gross_gmv"]),
                    "cnt": int(row["cnt"]),
                }
            )
    out.sort(key=lambda r: (r["bucket"], r["source"]))
    return out


def _bucket_label_from_date(d: date, granularity: str) -> str:
    if granularity == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if granularity == "month":
        return f"{d.year}-{d.month:02d}"
    return d.isoformat()


def _saas_timeseries_daily(start_d: date, end_d: date, granularity: str):
    """Approximate SaaS revenue per calendar day for charts."""
    daily = []
    cur = start_d
    subs = list(
        WidgetSubscription.objects.filter(
            status__in=("active", "trialing", "past_due"),
        )
    )
    monthly_by_sub = {}
    for sub in subs:
        m = _widget_subscription_monthly_amount(sub)
        if m is not None and m > 0:
            monthly_by_sub[sub.pk] = m
    while cur <= end_d:
        piece_total = Decimal("0")
        for sub in subs:
            monthly = monthly_by_sub.get(sub.pk)
            if monthly is None:
                continue
            span_start = max(cur, sub.created_at.date())
            span_end = min(cur, _subscription_end_date(sub, end_d))
            if span_start <= span_end:
                dim = monthrange(cur.year, cur.month)[1]
                piece_total += monthly / Decimal(dim)

        stripe_total = estimate_stripe_processing_fee(piece_total)
        daily.append(
            {
                "bucket": cur.isoformat(),
                "commission": piece_total.quantize(Decimal("0.01")),
                "commission_plus_tax": piece_total.quantize(Decimal("0.01")),
                "stripe_processing": stripe_total,
                "net_after_stripe": piece_total - stripe_total,
                "gross_gmv": piece_total,
                "cnt": 0,
            }
        )
        cur += timedelta(days=1)

    if granularity == "day":
        return daily

    merged = defaultdict(
        lambda: {
            "commission": Decimal("0"),
            "commission_plus_tax": Decimal("0"),
            "stripe_processing": Decimal("0"),
            "gross_gmv": Decimal("0"),
            "cnt": 0,
            "net_after_stripe": Decimal("0"),
        }
    )

    for row in daily:
        bk = _bucket_label_from_date(date.fromisoformat(row["bucket"]), granularity)
        merged[bk]["commission"] += row["commission"]
        merged[bk]["commission_plus_tax"] += row["commission_plus_tax"]
        merged[bk]["stripe_processing"] += row["stripe_processing"]
        merged[bk]["gross_gmv"] += row["gross_gmv"]
        merged[bk]["net_after_stripe"] += row["net_after_stripe"]

    series = []
    for bk in sorted(merged.keys()):
        row = merged[bk]
        row["bucket"] = bk
        series.append(row)
    return series


class PlatformRevenueOverviewAPIView(APIView):
    permission_classes = [IsAuthenticated, CanViewPlatformRevenue]

    def get(self, request):
        try:
            start_d, end_d = _daterange_params(
                request.query_params.get("start_date"),
                request.query_params.get("end_date"),
            )
            sources = _parse_sources(request.query_params.get("sources"))
            start_dt, end_dt = _to_dt_bounds(start_d, end_d)
            prev_start_d, prev_end_d = _previous_period_bounds(start_d, end_d)
            prev_start_dt, prev_end_dt = _to_dt_bounds(prev_start_d, prev_end_d)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        curr_sources = _sources_totals(start_dt, end_dt, sources)
        prev_sources = _sources_totals(prev_start_dt, prev_end_dt, sources)

        cad_totals = _cad_totals(curr_sources)
        cad_prev = _cad_totals(prev_sources)

        refunds = _refund_volume(start_dt, end_dt)

        base_curr = _payment_base_qs(start_dt, end_dt)
        marketplace_qs = base_curr.exclude(
            metadata__original_stripe_metadata__booking_source__in=WIDGET_SOURCE_LIST
        )
        widget_qs = base_curr.filter(
            metadata__original_stripe_metadata__booking_source__in=WIDGET_SOURCE_LIST
        )

        take_rate = _booking_take_rate(marketplace_qs, widget_qs)

        daily_series = _daily_payment_commissions(
            start_dt, end_dt, marketplace_qs, widget_qs
        )

        rolling_7 = None
        rolling_30 = None
        if len(daily_series) >= 7:
            rolling_7 = sum(row["commission"] for row in daily_series[-7:]) / 7.0
        if len(daily_series) >= 30:
            rolling_30 = sum(row["commission"] for row in daily_series[-30:]) / 30.0

        best_day = None
        worst_day = None
        if daily_series:
            best = max(daily_series, key=lambda r: r["commission"])
            worst = min(daily_series, key=lambda r: r["commission"])
            best_day = {"day": best["day"], "commission": best["commission"]}
            worst_day = {"day": worst["day"], "commission": worst["commission"]}

        new_biz_commission = Decimal("0")
        established_commission = Decimal("0")
        if SOURCE_MARKETPLACE in sources or SOURCE_WIDGET in sources:
            nb_filter = Q(
                booking__schedule_instance__schedule__option__classId__businessId__createdAt__gte=start_dt,
                booking__schedule_instance__schedule__option__classId__businessId__createdAt__lte=end_dt,
            )
            new_rows = marketplace_qs.filter(nb_filter) | widget_qs.filter(nb_filter)
            new_biz_commission = new_rows.aggregate(
                v=Coalesce(Sum("platform_fee_amount"), Decimal("0"))
            )["v"]
            all_rows = marketplace_qs | widget_qs
            established_commission = (
                all_rows.aggregate(v=Coalesce(Sum("platform_fee_amount"), Decimal("0")))["v"]
                - new_biz_commission
            )

        nb_pct = None
        total_booking_comm = marketplace_qs.aggregate(
            v=Coalesce(Sum("platform_fee_amount"), Decimal("0"))
        )["v"] + widget_qs.aggregate(v=Coalesce(Sum("platform_fee_amount"), Decimal("0")))["v"]
        if total_booking_comm > 0:
            nb_pct = float((new_biz_commission / total_booking_comm) * Decimal("100"))

        serialized_sources = {}
        for sk, sv in curr_sources.items():
            serialized_sources[sk] = _serialize_money_block(sv)
            serialized_sources[sk]["currency"] = sv.get("currency", "CAD")

        cad_block = _serialize_money_block(cad_totals)
        cad_prev_block = _serialize_money_block(cad_prev)

        resp = {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "sources": sorted(sources),
            "kpis_cad": cad_block,
            "kpis_cad_previous_period": cad_prev_block,
            "deltas_vs_previous_period_cad": {
                "commission_pct": _pct_delta(cad_prev["commission"], cad_totals["commission"]),
                "commission_plus_tax_pct": _pct_delta(
                    cad_prev["commission_plus_tax"], cad_totals["commission_plus_tax"]
                ),
                "net_after_stripe_pct": _pct_delta(
                    cad_prev["net_after_stripe"], cad_totals["net_after_stripe"]
                ),
            },
            "by_source": serialized_sources,
            "averages_cad": {
                "per_day": _f(cad_totals["commission"] / Decimal(max(1, (end_d - start_d).days + 1))),
                "per_transaction": _f(
                    cad_totals["commission"] / Decimal(max(1, cad_totals["cnt"]))
                ),
            },
            "refunds_volume": _f(refunds),
            "advanced": {
                "booking_take_rate_percent": take_rate,
                "rolling_avg_commission_per_day_7d": rolling_7,
                "rolling_avg_commission_per_day_30d": rolling_30,
                "best_day": best_day,
                "worst_day": worst_day,
                "new_business_booking_commission": _f(new_biz_commission),
                "established_business_booking_commission": _f(established_commission),
                "new_business_booking_commission_pct_of_bookings": nb_pct,
            },
            "meta": {
                "stripe_processing_estimate": True,
                "corporate_fee_percent": float(_corp_fee_pct_decimal() * Decimal("100")),
                "currency": "CAD",
            },
        }

        return Response(resp)


class PlatformRevenueTimeseriesAPIView(APIView):
    permission_classes = [IsAuthenticated, CanViewPlatformRevenue]

    def get(self, request):
        try:
            start_d, end_d = _daterange_params(
                request.query_params.get("start_date"),
                request.query_params.get("end_date"),
            )
            sources = _parse_sources(request.query_params.get("sources"))
            granularity = _parse_granularity(request.query_params.get("granularity"))
            start_dt, end_dt = _to_dt_bounds(start_d, end_d)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        trunc = _trunc_cls(granularity)
        by_source: dict[str, list] = {}

        base = _payment_base_qs(start_dt, end_dt)
        marketplace_qs = base.exclude(
            metadata__original_stripe_metadata__booking_source__in=WIDGET_SOURCE_LIST
        )
        widget_qs = base.filter(
            metadata__original_stripe_metadata__booking_source__in=WIDGET_SOURCE_LIST
        )

        if SOURCE_MARKETPLACE in sources:
            by_source[SOURCE_MARKETPLACE] = _series_payment_buckets(
                marketplace_qs, trunc, "created_at"
            )
        if SOURCE_WIDGET in sources:
            by_source[SOURCE_WIDGET] = _series_payment_buckets(widget_qs, trunc, "created_at")
        if SOURCE_MEMBERSHIP in sources:
            by_source[SOURCE_MEMBERSHIP] = _series_membership_buckets(start_dt, end_dt, trunc)
        if SOURCE_CORPORATE in sources:
            by_source[SOURCE_CORPORATE] = _series_corporate_buckets(start_dt, end_dt, trunc)

        if SOURCE_SAAS in sources:
            by_source[SOURCE_SAAS] = _saas_timeseries_daily(start_d, end_d, granularity)

        if SOURCE_ADDON in sources:
            addon_daily = []
            cur = start_d
            qs = list(
                BusinessAddonSubscription.objects.filter(
                    addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                    status__in=("active", "trialing", "past_due"),
                )
            )
            monthly_by_addon_sub = {}
            for sub in qs:
                m = _addon_subscription_monthly_amount(sub)
                if m is not None and m > 0:
                    monthly_by_addon_sub[sub.pk] = m
            while cur <= end_d:
                piece_total = Decimal("0")
                for sub in qs:
                    monthly = monthly_by_addon_sub.get(sub.pk)
                    if monthly is None:
                        continue
                    span_start = max(cur, sub.created_at.date())
                    span_end = min(cur, _subscription_end_date(sub, end_d))
                    if span_start <= span_end:
                        dim = monthrange(cur.year, cur.month)[1]
                        piece_total += monthly / Decimal(dim)
                stripe_total = estimate_stripe_processing_fee(piece_total)
                addon_daily.append(
                    {
                        "bucket": cur.isoformat(),
                        "commission": piece_total.quantize(Decimal("0.01")),
                        "commission_plus_tax": piece_total.quantize(Decimal("0.01")),
                        "stripe_processing": stripe_total,
                        "net_after_stripe": piece_total - stripe_total,
                        "gross_gmv": piece_total,
                        "cnt": 0,
                    }
                )
                cur += timedelta(days=1)

            if granularity == "day":
                by_source[SOURCE_ADDON] = addon_daily
            else:
                merged_addon = defaultdict(
                    lambda: {
                        "commission": Decimal("0"),
                        "commission_plus_tax": Decimal("0"),
                        "stripe_processing": Decimal("0"),
                        "gross_gmv": Decimal("0"),
                        "cnt": 0,
                        "net_after_stripe": Decimal("0"),
                    }
                )

                for row in addon_daily:
                    bk = _bucket_label_from_date(
                        date.fromisoformat(row["bucket"]), granularity
                    )
                    merged_addon[bk]["commission"] += row["commission"]
                    merged_addon[bk]["commission_plus_tax"] += row["commission_plus_tax"]
                    merged_addon[bk]["stripe_processing"] += row["stripe_processing"]
                    merged_addon[bk]["gross_gmv"] += row["gross_gmv"]
                    merged_addon[bk]["net_after_stripe"] += row["net_after_stripe"]

                by_source[SOURCE_ADDON] = [
                    {"bucket": bk, **vals} for bk, vals in sorted(merged_addon.items())
                ]

        rows = _merge_timeseries(by_source)
        return Response(
            {
                "granularity": granularity,
                "start_date": start_d.isoformat(),
                "end_date": end_d.isoformat(),
                "rows": rows,
            }
        )


class PlatformRevenueTopAPIView(APIView):
    permission_classes = [IsAuthenticated, CanViewPlatformRevenue]

    def get(self, request):
        try:
            start_d, end_d = _daterange_params(
                request.query_params.get("start_date"),
                request.query_params.get("end_date"),
            )
            start_dt, end_dt = _to_dt_bounds(start_d, end_d)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        limit = int(request.query_params.get("limit") or 10)
        limit = max(1, min(limit, 50))

        metric = (request.query_params.get("metric") or "commission").lower()
        pay_field = "platform_fee_amount"
        if metric == "gross_gmv":
            pay_field = "amount"

        base = _payment_base_qs(start_dt, end_dt)

        biz_field = (
            "booking__schedule_instance__schedule__option__classId__businessId_id"
        )

        top_businesses_raw = (
            base.values(biz_field)
            .annotate(total=Sum(pay_field), bookings=Count("id"))
            .order_by("-total")[:limit]
        )

        spark_start = end_dt - timedelta(days=6)
        spark_qs = base.filter(created_at__gte=spark_start, created_at__lte=end_dt)

        biz_ids = [row[biz_field] for row in top_businesses_raw if row[biz_field]]

        spark_map = defaultdict(lambda: [Decimal("0")] * 7)
        if biz_ids:
            for row in (
                spark_qs.filter(**{f"{biz_field}__in": biz_ids})
                .annotate(day=TruncDate("created_at"))
                .values(biz_field, "day")
                .annotate(daily_total=Sum("platform_fee_amount"))
            ):
                if not row["day"]:
                    continue
                day_val = row["day"]
                if hasattr(day_val, "date"):
                    day_val = day_val.date()
                idx = (day_val - spark_start.date()).days
                if 0 <= idx < 7:
                    spark_map[row[biz_field]][idx] = row["daily_total"] or Decimal("0")

        names = dict(
            BusinessInfo.objects.filter(businessId__in=biz_ids).values_list(
                "businessId", "businessName"
            )
        )

        top_businesses = []
        for row in top_businesses_raw:
            bid = row[biz_field]
            if not bid:
                continue
            top_businesses.append(
                {
                    "business_id": bid,
                    "name": names.get(bid) or "Unknown",
                    "total": _f(row["total"]),
                    "transactions": row["bookings"],
                    "sparkline_commission": [_f(x) for x in spark_map[bid]],
                }
            )

        top_classes_raw = (
            base.values(
                "booking__schedule_instance__schedule__option__classId_id",
                "booking__schedule_instance__schedule__option__classId__title",
            )
            .annotate(total=Sum(pay_field), bookings=Count("id"))
            .order_by("-total")[:limit]
        )

        top_classes = []
        for row in top_classes_raw:
            cid = row["booking__schedule_instance__schedule__option__classId_id"]
            if not cid:
                continue
            top_classes.append(
                {
                    "class_id": cid,
                    "title": row[
                        "booking__schedule_instance__schedule__option__classId__title"
                    ]
                    or "",
                    "total": _f(row["total"]),
                    "transactions": row["bookings"],
                }
            )

        gross_usd_d, comm_d, _, _, _ = _deposit_corporate_exprs()
        gross_usd_b, comm_b, _, _, _ = _balance_corporate_exprs()

        corp_rows_dep = (
            CorporateBooking.objects.filter(
                deposit_paid_at__gte=start_dt,
                deposit_paid_at__lte=end_dt,
            )
            .values(
                "billing_company_name",
                "shortlist__inquiry__company_name",
            )
            .annotate(total=Sum(comm_d))
        )
        corp_rows_bal = (
            CorporateBooking.objects.filter(
                balance_paid_at__gte=start_dt,
                balance_paid_at__lte=end_dt,
            )
            .values(
                "billing_company_name",
                "shortlist__inquiry__company_name",
            )
            .annotate(total=Sum(comm_b))
        )

        corp_accum = defaultdict(Decimal)
        for row in corp_rows_dep:
            name = row["billing_company_name"] or row["shortlist__inquiry__company_name"]
            corp_accum[name] += row["total"] or Decimal("0")
        for row in corp_rows_bal:
            name = row["billing_company_name"] or row["shortlist__inquiry__company_name"]
            corp_accum[name] += row["total"] or Decimal("0")

        corporate_clients = sorted(
            [{"name": k, "total": _f(v)} for k, v in corp_accum.items()],
            key=lambda x: x["total"],
            reverse=True,
        )[:limit]

        mem_rows = (
            _membership_base_qs(start_dt, end_dt)
            .values("membership__product__business_id")
            .annotate(total=Sum("platform_fee_amount"))
            .order_by("-total")[:limit]
        )
        mem_business_ids = [r["membership__product__business_id"] for r in mem_rows]
        mem_names = dict(
            BusinessInfo.objects.filter(businessId__in=mem_business_ids).values_list(
                "businessId", "businessName"
            )
        )

        top_membership_businesses = [
            {
                "business_id": r["membership__product__business_id"],
                "name": mem_names.get(r["membership__product__business_id"]) or "Unknown",
                "total": _f(r["total"]),
            }
            for r in mem_rows
            if r["membership__product__business_id"]
        ]

        return Response(
            {
                "top_businesses_by_booking_revenue": top_businesses,
                "top_classes": top_classes,
                "corporate_clients": corporate_clients,
                "top_membership_businesses": top_membership_businesses,
            }
        )


class PlatformRevenueExportAPIView(APIView):
    permission_classes = [IsAuthenticated, CanViewPlatformRevenue]

    def post(self, request):
        try:
            body = request.data if isinstance(request.data, dict) else {}
            start_d, end_d = _daterange_params(body.get("start_date"), body.get("end_date"))
            sources = _parse_sources(body.get("sources"))
            start_dt, end_dt = _to_dt_bounds(start_d, end_d)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        totals_by_source = _sources_totals(start_dt, end_dt, sources)
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "source",
                "currency",
                "commission",
                "commission_plus_tax",
                "net_after_stripe_estimate",
                "gross_gmv",
                "transactions",
            ]
        )
        for src in sorted(totals_by_source.keys()):
            row = totals_by_source[src]
            writer.writerow(
                [
                    src,
                    row.get("currency", ""),
                    row["commission"],
                    row["commission_plus_tax"],
                    row["net_after_stripe"],
                    row["gross_gmv"],
                    row["cnt"],
                ]
            )

        resp = HttpResponse(buf.getvalue(), content_type="text/csv")
        resp["Content-Disposition"] = (
            f'attachment; filename="platform-revenue-{start_d.isoformat()}-{end_d.isoformat()}.csv"'
        )
        return resp
