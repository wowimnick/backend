from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.conf import settings
from datetime import timedelta, datetime
from decimal import Decimal
from collections import defaultdict
import hashlib
import stripe
import logging
import pytz

from quickstart.models import Booking, Payout, BusinessInfo
from quickstart.utils.notification_utils import create_notification_for_recipients
from quickstart.utils.stripe_metadata import stripe_metadata_to_dict
from quickstart.utils.stripe_refund import create_stripe_refund

# CUTOVER_DATE = 2026-08-31
# Destination-charge bookings (payout_status='settled') skip this holding-account batch.
# Booking uses booking_date (no created_at). Only pre-cutover pending rows are included.
CUTOVER_DATE = datetime(2026, 8, 31, tzinfo=timezone.utc)

logger = logging.getLogger(__name__)
PAYOUT_INTEGRITY_ALERT_RECIPIENTS = ("nick@classeasily.com",)
PAYOUT_INTEGRITY_PENDING_QUEUE_WARNING_BUSINESSES = 25
PAYOUT_INTEGRITY_PENDING_QUEUE_WARNING_TOTAL_ALLOC = Decimal("5000.00")

_CONNECT_NOT_READY_STATUSES = frozenset(
    {"incomplete", "restricted", "pending", "unlinked"}
)


def _pending_payout_totals_for_business(business):
    """
    Sum allocated_net_payout and count bookings eligible for payout for this business
    (completed/forfeited, paid, pending payout, allocated > 0).
    """
    pending_bookings = Booking.objects.filter(
        schedule_instance__schedule__option__classId__businessId=business,
        status__in=["completed", "forfeited"],
        payment_status="paid",
        payout_status="pending",
    )
    total_payout = Decimal("0.00")
    booking_count = 0
    for booking in pending_bookings:
        allocated = booking.allocated_net_payout or Decimal("0.00")
        if allocated <= 0:
            continue
        total_payout += allocated
        booking_count += 1
    return total_payout, booking_count


def _stripe_transfer_destination_not_ready(stripe_error: stripe.StripeError) -> bool:
    """True when Stripe indicates the connected account cannot receive transfers yet."""
    msg = (str(stripe_error) or "").lower()
    code = (getattr(stripe_error, "code", None) or "").lower()
    if code in ("balance_insufficient",):
        return False
    needles = (
        "transfers are not enabled",
        "transfer cannot be created",
        "does not have transfers enabled",
        "does not have the ability to receive transfers",
        "cannot create a transfer",
        "connected account is not ready",
        "charges not enabled",
        "payouts are not enabled",
        "invalid destination",
        "destination account cannot",
        "account restricted",
    )
    return any(n in msg for n in needles)


def _is_business_connect_not_ready_for_payout(
    business: BusinessInfo, stripe_error: stripe.StripeError
) -> bool:
    """
    Payout failure is the business's responsibility (finish Connect / resolve restrictions),
    not an internal platform error — route to connect-required email instead of super admins.
    """
    status = (business.stripe_account_status or "").strip().lower()
    if status in _CONNECT_NOT_READY_STATUSES:
        return True
    return _stripe_transfer_destination_not_ready(stripe_error)


def _send_payout_connect_required_immediate(business: BusinessInfo) -> None:
    """
    Email the business to finish Connect / payout setup after a transfer failure.
    Uses the same 3-day cooldown as scheduled reminders.
    """
    from quickstart.utils.email_utils import send_payout_connect_required_email

    total_payout, booking_count = _pending_payout_totals_for_business(business)
    if booking_count == 0:
        logger.warning(
            "Payout connect email skipped: Business %s has no eligible pending bookings.",
            business.businessId,
        )
        return

    now = timezone.now()
    last_sent = getattr(business, "last_payout_connect_reminder_sent", None)
    if last_sent and (now - last_sent) < timedelta(days=3):
        logger.info(
            "Payout connect reminder cooldown: Business %s last sent %s. Skipping immediate email.",
            business.businessId,
            last_sent,
        )
        return

    owner = business.owner
    if not owner or not owner.email:
        logger.warning(
            "Business %s has no owner email for immediate payout connect email.",
            business.businessId,
        )
        return

    send_payout_connect_required_email(
        business_user=owner,
        pending_amount=total_payout,
        booking_count=booking_count,
        currency=business.currency or "CAD",
    )
    BusinessInfo.objects.filter(pk=business.businessId).update(
        last_payout_connect_reminder_sent=now
    )
    logger.info(
        "Sent immediate payout connect required email to %s for Business %s "
        "(pending_amount=$%s, booking_count=%s)",
        owner.email,
        business.businessId,
        total_payout,
        booking_count,
    )


def _maybe_send_payout_connect_reminder(business):
    """
    If the business has pending payout bookings and we haven't sent the
    "connect Stripe" reminder in the last 3 days, send it and update the cooldown.
    """
    from quickstart.utils.email_utils import send_payout_connect_required_email

    total_payout, booking_count = _pending_payout_totals_for_business(business)
    if booking_count == 0:
        return

    last_sent = getattr(business, "last_payout_connect_reminder_sent", None)
    if last_sent and (timezone.now() - last_sent) < timedelta(days=3):
        logger.info(
            f"Payout connect reminder cooldown: Business {business.businessId} "
            f"last sent {last_sent}. Skipping."
        )
        return

    owner = business.owner
    if not owner or not owner.email:
        logger.warning(
            f"Business {business.businessId} has no owner email for payout connect reminder."
        )
        return

    send_payout_connect_required_email(
        business_user=owner,
        pending_amount=total_payout,
        booking_count=booking_count,
        currency=business.currency or "CAD",
    )
    BusinessInfo.objects.filter(pk=business.businessId).update(
        last_payout_connect_reminder_sent=timezone.now()
    )
    logger.info(
        f"Sent payout connect required email to {owner.email} for Business {business.businessId} "
        f"(pending_amount=${total_payout}, booking_count={booking_count})"
    )


def _build_payout_batch_idempotency_key(
    business_id: int, currency: str, booking_ids: list[int], payout_amount_cents: int
) -> str:
    """
    Deterministic key for a specific payout batch to make Stripe transfer retries safe.
    """
    normalized_ids = ",".join(str(i) for i in sorted(booking_ids))
    payload = f"{business_id}|{currency.upper()}|{payout_amount_cents}|{normalized_ids}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"payout:{business_id}:{currency.upper()}:{payout_amount_cents}:{digest}"


def _build_stripe_transfer_metadata(
    business_id: int, booking_count: int, idempotency_key: str
) -> dict:
    """
    Stripe transfer metadata must stay identical across retries for the same batch.
    Do not include values that change per attempt (e.g. payout_record_id).
    """
    return {
        "business_id": str(business_id),
        "booking_count": str(booking_count),
        "batch_idempotency_key": idempotency_key,
    }


def _is_stripe_idempotency_key_conflict(stripe_error: stripe.StripeError) -> bool:
    code = (getattr(stripe_error, "code", None) or "").lower()
    if code in ("idempotency_key_in_use",):
        return True
    msg = (str(stripe_error) or "").lower()
    return "idempotent" in msg and "same parameters" in msg


def _get_completed_payout_for_batch(idempotency_key: str):
    return (
        Payout.objects.filter(metadata__batch_idempotency_key=idempotency_key)
        .exclude(stripe_transfer_id__startswith="temp_")
        .exclude(stripe_transfer_id="")
        .order_by("-created_at")
        .first()
    )


def _find_stripe_transfer_for_batch(
    *,
    destination: str,
    idempotency_key: str,
    amount_cents: int,
    currency: str,
    business_id: int,
):
    """
    Locate an existing Stripe transfer for a payout batch (orphan recovery).
    """
    if not destination:
        return None

    starting_after = None
    for _ in range(10):
        list_kwargs = {"limit": 100, "destination": destination}
        if starting_after:
            list_kwargs["starting_after"] = starting_after
        page = stripe.Transfer.list(**list_kwargs)
        transfers = getattr(page, "data", None) or []
        if not transfers:
            break

        for transfer in transfers:
            transfer_id = getattr(transfer, "id", "") or ""
            if not transfer_id.startswith("tr_"):
                continue
            transfer_amount = int(getattr(transfer, "amount", 0) or 0)
            transfer_currency = (getattr(transfer, "currency", "") or "").lower()
            if transfer_amount != amount_cents or transfer_currency != currency.lower():
                continue

            meta = stripe_metadata_to_dict(getattr(transfer, "metadata", None))
            batch_key = meta.get("batch_idempotency_key")
            if batch_key == idempotency_key:
                return transfer

            description = getattr(transfer, "description", "") or ""
            if (
                batch_key is None
                and str(meta.get("business_id")) == str(business_id)
                and "ClassEasily Payout" in description
            ):
                return transfer

        if not getattr(page, "has_more", False):
            break
        starting_after = transfers[-1].id

    return None


def _create_stripe_transfer_for_batch(
    *,
    business: BusinessInfo,
    payout_amount_cents: int,
    idempotency_key: str,
    booking_count: int,
):
    """
    Create (or recover) a Stripe transfer for a payout batch.
    """
    stable_metadata = _build_stripe_transfer_metadata(
        business.businessId, booking_count, idempotency_key
    )
    transfer_params = {
        "amount": payout_amount_cents,
        "currency": business.currency.lower(),
        "destination": business.stripe_account_id,
        "description": "ClassEasily Payout",
        "metadata": stable_metadata,
    }

    try:
        return stripe.Transfer.create(
            **transfer_params,
            idempotency_key=idempotency_key,
        )
    except stripe.StripeError as stripe_error:
        if not _is_stripe_idempotency_key_conflict(stripe_error):
            raise

        logger.warning(
            "Stripe idempotency conflict for Business %s batch %s; searching for existing transfer.",
            business.businessId,
            idempotency_key,
        )
        existing_transfer = _find_stripe_transfer_for_batch(
            destination=business.stripe_account_id,
            idempotency_key=idempotency_key,
            amount_cents=payout_amount_cents,
            currency=business.currency,
            business_id=business.businessId,
        )
        if existing_transfer is not None:
            logger.info(
                "Recovered existing Stripe transfer %s for batch %s",
                existing_transfer.id,
                idempotency_key,
            )
            return existing_transfer

        recovery_key = f"{idempotency_key}:v2"
        logger.info(
            "No existing Stripe transfer for batch %s; retrying with recovery key %s",
            idempotency_key,
            recovery_key,
        )
        return stripe.Transfer.create(
            **transfer_params,
            idempotency_key=recovery_key,
        )


def _finalize_successful_payout(
    payout_record: Payout,
    transfer,
    booking_ids: list[int],
):
    arrival_date = timezone.now().date()

    base_meta = payout_record.metadata
    if not isinstance(base_meta, dict):
        base_meta = {}
    updated_metadata = dict(base_meta)
    updated_metadata.update(
        stripe_metadata_to_dict(getattr(transfer, "metadata", None))
    )
    updated_metadata["payout_record_id"] = str(payout_record.id)
    updated_metadata.pop("temp_id", None)

    payout_record.stripe_transfer_id = transfer.id
    payout_record.arrival_date = arrival_date
    payout_record.status = "paid"
    payout_record.metadata = updated_metadata
    payout_record.save(
        update_fields=[
            "stripe_transfer_id",
            "arrival_date",
            "status",
            "metadata",
        ]
    )

    Booking.objects.filter(id__in=booking_ids).update(payout_status="processed")
    payout_record.bookings.add(*booking_ids)


def _reconcile_pending_bookings_to_existing_payout(
    payout_record: Payout,
    booking_ids: list[int],
) -> int:
    """
    Link any still-pending bookings to an already-completed payout for this batch.
    """
    pending_ids = list(
        Booking.objects.filter(id__in=booking_ids, payout_status="pending").values_list(
            "id", flat=True
        )
    )
    if not pending_ids:
        return 0

    Booking.objects.filter(id__in=pending_ids).update(payout_status="processed")
    payout_record.bookings.add(*pending_ids)
    logger.info(
        "Reconciled %s pending booking(s) to existing payout %s",
        len(pending_ids),
        payout_record.id,
    )
    return len(pending_ids)


def collect_payout_integrity_findings(
    *, days: int = 3, stripe_limit: int = 200, include_stripe: bool = True
) -> dict:
    """
    Read-only payout integrity checks used by both scheduled task and management command.
    Returns a dict with summary counts and anomaly details.
    """
    now = timezone.now()
    since = now - timedelta(days=max(1, int(days)))

    payouts = list(
        Payout.objects.filter(created_at__gte=since).select_related("business")
    )

    duplicate_bucket = defaultdict(list)
    temp_like_rows = []
    for payout in payouts:
        created_day = payout.created_at.date().isoformat()
        destination = getattr(payout.business, "stripe_account_id", "") or ""
        key = (
            payout.business_id,
            str(payout.amount),
            (payout.currency or "").upper(),
            created_day,
            destination,
        )
        duplicate_bucket[key].append(payout)
        meta = payout.metadata if isinstance(payout.metadata, dict) else {}
        if str(payout.stripe_transfer_id).startswith("temp_") or meta.get("temp_id"):
            temp_like_rows.append(payout)

    duplicate_like_groups = []
    for key, rows in duplicate_bucket.items():
        if len(rows) <= 1:
            continue
        bid, amount, currency, created_day, destination = key
        duplicate_like_groups.append(
            {
                "business_id": bid,
                "amount": amount,
                "currency": currency,
                "day": created_day,
                "destination": destination,
                "count": len(rows),
                "payout_ids": [str(r.id) for r in rows],
                "transfer_ids": [r.stripe_transfer_id for r in rows],
            }
        )

    pending_rows = list(
        Booking.objects.filter(
            status__in=["completed", "forfeited"],
            payment_status="paid",
            payout_status="pending",
        )
        .values("schedule_instance__schedule__option__classId__businessId")
        .annotate(c=Count("id"), total_alloc=Sum("allocated_net_payout"))
        .order_by("-total_alloc")
    )
    pending_rows_with_bookings = [row for row in pending_rows if row["c"] > 0]
    pending_queue = [
        {
            "business_id": row[
                "schedule_instance__schedule__option__classId__businessId"
            ],
            "pending_bookings": row["c"],
            "sum_allocated_net_payout": str(
                (row["total_alloc"] or Decimal("0")).quantize(Decimal("0.01"))
            ),
        }
        for row in pending_rows_with_bookings
    ]

    db_transfer_ids = set(
        Payout.objects.exclude(stripe_transfer_id__startswith="temp_")
        .exclude(stripe_transfer_id="")
        .values_list("stripe_transfer_id", flat=True)
    )
    orphan_transfers = []
    stripe_api_errors = []
    if include_stripe:
        try:
            tr_list = stripe.Transfer.list(limit=max(1, min(int(stripe_limit), 500)))
            for tr in getattr(tr_list, "data", []) or []:
                tid = getattr(tr, "id", "") or ""
                if not tid.startswith("tr_"):
                    continue
                description = getattr(tr, "description", "") or ""
                if "ClassEasily Payout" not in description:
                    continue
                created_ts = getattr(tr, "created", None)
                if created_ts:
                    created_dt = datetime.fromtimestamp(int(created_ts), tz=pytz.utc)
                    if created_dt < since.astimezone(pytz.utc):
                        continue
                if tid in db_transfer_ids:
                    continue
                amount = Decimal(str(getattr(tr, "amount", 0) or 0)) / Decimal("100")
                orphan_transfers.append(
                    {
                        "transfer_id": tid,
                        "amount": str(amount.quantize(Decimal("0.01"))),
                        "currency": (getattr(tr, "currency", "") or "").upper(),
                        "destination": getattr(tr, "destination", "") or "",
                        "created": created_dt.isoformat() if created_ts else "",
                        "description": description,
                    }
                )
        except stripe.StripeError as stripe_err:
            stripe_api_errors.append(str(stripe_err))

    pending_total_alloc = sum(
        (row["total_alloc"] or Decimal("0") for row in pending_rows_with_bookings),
        start=Decimal("0"),
    ).quantize(Decimal("0.01"))

    critical_findings = []
    warning_findings = []
    if duplicate_like_groups:
        critical_findings.append(
            f"Duplicate-like payout groups: {len(duplicate_like_groups)}"
        )
    if temp_like_rows:
        critical_findings.append(
            f"Payout rows still using temporary transfer ids: {len(temp_like_rows)}"
        )
    if orphan_transfers:
        critical_findings.append(
            f"Stripe transfers missing DB payout rows: {len(orphan_transfers)}"
        )
    if stripe_api_errors:
        warning_findings.append(
            f"Stripe API errors while auditing transfers: {len(stripe_api_errors)}"
        )
    if (
        len(pending_queue) >= PAYOUT_INTEGRITY_PENDING_QUEUE_WARNING_BUSINESSES
        or pending_total_alloc >= PAYOUT_INTEGRITY_PENDING_QUEUE_WARNING_TOTAL_ALLOC
    ):
        warning_findings.append(
            "Pending payout queue elevated: "
            f"{len(pending_queue)} businesses, total ${pending_total_alloc}"
        )

    findings = critical_findings + warning_findings

    return {
        "ran_at": now.isoformat(),
        "days": int(days),
        "stripe_limit": int(stripe_limit),
        "findings": findings,
        "critical_findings": critical_findings,
        "warning_findings": warning_findings,
        "duplicate_like_groups": duplicate_like_groups,
        "temp_like_rows": [
            {
                "payout_id": str(p.id),
                "business_id": p.business_id,
                "stripe_transfer_id": p.stripe_transfer_id,
                "created_at": p.created_at.isoformat(),
            }
            for p in temp_like_rows
        ],
        "orphan_transfers": orphan_transfers,
        "stripe_api_errors": stripe_api_errors,
        "pending_queue_top": pending_queue[:10],
        "pending_queue_businesses": len(pending_queue),
        "pending_queue_total_alloc": str(pending_total_alloc),
    }


def _send_payout_integrity_alert_email(report: dict, *, include_warnings: bool = False) -> None:
    from quickstart.utils.email_utils import send_templated_email

    critical_findings = report.get("critical_findings") or []
    warning_findings = report.get("warning_findings") or []
    findings = critical_findings + warning_findings if include_warnings else critical_findings
    if not findings:
        return

    if include_warnings:
        subject = (
            "[ClassEasily] Daily payout integrity digest: "
            f"{len(critical_findings)} critical, {len(warning_findings)} warning"
        )
        alert_mode = "daily_digest"
    else:
        subject = (
            "[ClassEasily] Payout integrity CRITICAL alert: "
            f"{len(critical_findings)} issue(s) detected"
        )
        alert_mode = "critical_only"

    send_templated_email(
        recipient_list=list(PAYOUT_INTEGRITY_ALERT_RECIPIENTS),
        template_name="emails/payout_integrity_alert.html",
        context={
            "report": report,
            "findings": findings,
            "critical_findings": critical_findings,
            "warning_findings": warning_findings,
            "alert_mode": alert_mode,
            "admin_url": f"{settings.FRONTEND_BASE_URL}/admin",
            "recipient_email": ", ".join(PAYOUT_INTEGRITY_ALERT_RECIPIENTS),
        },
        subject=subject,
    )
    logger.warning(
        "Payout integrity alert sent to %s with %s findings.",
        ",".join(PAYOUT_INTEGRITY_ALERT_RECIPIENTS),
        len(findings),
    )


@shared_task
def monitor_payout_integrity(days: int = 3, stripe_limit: int = 200):
    """
    Scheduled payout audit every 6 hours.
    Sends emails only for CRITICAL findings.
    """
    report = collect_payout_integrity_findings(
        days=days, stripe_limit=stripe_limit, include_stripe=True
    )
    critical_findings = report.get("critical_findings") or []
    warning_findings = report.get("warning_findings") or []
    if critical_findings:
        _send_payout_integrity_alert_email(report)
        return (
            f"Payout integrity CRITICAL anomalies found: {len(critical_findings)}. "
            f"Alert sent to {','.join(PAYOUT_INTEGRITY_ALERT_RECIPIENTS)}."
        )
    if warning_findings:
        logger.warning(
            "Payout integrity warning-only run (no immediate email): %s",
            "; ".join(warning_findings),
        )
        return (
            f"Payout integrity warning-only findings: {len(warning_findings)}. "
            "No immediate email sent."
        )
    logger.info(
        "Payout integrity check clean (days=%s, stripe_limit=%s).",
        days,
        stripe_limit,
    )
    return "Payout integrity check clean."


@shared_task
def send_daily_payout_integrity_warning_digest(days: int = 7, stripe_limit: int = 300):
    """
    Daily digest for payout integrity warnings (and any criticals) to keep visibility
    without alert fatigue from non-critical findings.
    """
    report = collect_payout_integrity_findings(
        days=days, stripe_limit=stripe_limit, include_stripe=True
    )
    critical_findings = report.get("critical_findings") or []
    warning_findings = report.get("warning_findings") or []
    if critical_findings or warning_findings:
        _send_payout_integrity_alert_email(report, include_warnings=True)
        return (
            "Daily payout integrity digest sent: "
            f"{len(critical_findings)} critical, {len(warning_findings)} warning."
        )
    logger.info(
        "Daily payout integrity digest clean (days=%s, stripe_limit=%s).",
        days,
        stripe_limit,
    )
    return "Daily payout integrity digest clean."


def _on_cancellation_refund_failed(booking, reason):
    """
    When a cancellation refund could not be completed: log critical for internal alert
    and optionally email the guest that support will follow up.
    """
    logger.critical(
        "REFUND_FAILED: Booking %s - Cancellation refund could not be completed. Reason: %s. Manual follow-up required.",
        booking.id,
        reason,
    )
    try:
        from quickstart.utils.email_utils import send_refund_failed_guest_email
        send_refund_failed_guest_email(booking, reason)
    except Exception as e:
        logger.warning("Could not send refund failed email to guest for booking %s: %s", booking.id, e)


stripe.api_key = settings.STRIPE_SECRET_KEY


@shared_task
def update_completed_booking_status():
    """
    Marks past, confirmed bookings as 'completed' so they are eligible for payout.
    """
    logger.info("=" * 80)
    logger.info("TASK START: update_completed_booking_status")
    logger.info("=" * 80)
    
    yesterday = timezone.now().date() - timedelta(days=1)
    logger.info(f"Looking for bookings on or before: {yesterday}")
    
    instances_to_complete = Q(schedule_instance__date__lte=yesterday)
    bookings_to_update = Booking.objects.filter(
        instances_to_complete, status="confirmed"
    )

    count_to_update = bookings_to_update.count()
    logger.info(f"Found {count_to_update} confirmed bookings to mark as completed")

    try:
        per_business = (
            bookings_to_update.values(
                "schedule_instance__schedule__option__classId__businessId"
            )
            .annotate(n=Count("id"))
            .order_by()
        )
        for row in per_business:
            bid = row["schedule_instance__schedule__option__classId__businessId"]
            n = row["n"]
            if not bid or n < 1:
                continue
            try:
                biz = BusinessInfo.objects.get(pk=bid)
            except BusinessInfo.DoesNotExist:
                continue
            done_msg = (
                f"You have {n} completed class(es) ready for payout."
                if n != 1
                else "You have 1 completed class ready for payout."
            )
            create_notification_for_recipients(
                biz,
                "booking_completed",
                done_msg,
                "CheckCircle",
                "#22c55e",
                "/business/dashboard?tab=payouts",
            )
    except Exception as e:
        logger.warning(
            "booking_completed in-app notifications skipped: %s", e, exc_info=True
        )

    updated_count = bookings_to_update.update(status="completed")
    
    logger.info(f"Successfully marked {updated_count} past bookings as 'completed'")
    logger.info("=" * 80)
    logger.info("TASK END: update_completed_booking_status")
    logger.info("=" * 80)
    
    return f"Completed status update. {updated_count} bookings marked as 'completed'."

@shared_task
def process_daily_payouts():
    """
    Groups paid-out bookings by business and initiates Stripe transfers.
    Uses database locking to prevent race conditions (double payouts).
    """
    logger.info("=" * 80)
    logger.info("TASK START: process_daily_payouts")
    logger.info(f"Task triggered at: {timezone.now()}")
    logger.info("=" * 80)

    # 1. Identify which businesses have pending payouts
    # We do a lightweight query first to get the IDs, avoiding a massive lock.
    business_ids_with_pending = Booking.objects.filter(
        status__in=["completed", "forfeited"],
        payment_status="paid",
        payout_status="pending",
        booking_date__lt=CUTOVER_DATE,
    ).exclude(payout_status="settled").values_list(
        'schedule_instance__schedule__option__classId__businessId', flat=True
    ).distinct()

    if not business_ids_with_pending:
        logger.info("No bookings found requiring payout. Process finished.")
        logger.info("=" * 80)
        logger.info("TASK END: process_daily_payouts (No work to do)")
        logger.info("=" * 80)
        return "No bookings to pay out."
    
    logger.info(f"Found {len(business_ids_with_pending)} businesses with pending payouts to process")
    logger.info(f"Business IDs: {list(business_ids_with_pending)}")

    successful_payouts = 0
    failed_payouts = 0

    # 2. Process each business individually with a transaction lock
    for idx, business_id in enumerate(business_ids_with_pending, 1):
        logger.info(f"--- Processing Business {idx}/{len(business_ids_with_pending)}: ID={business_id} ---")
        try:
            with transaction.atomic():
                # Fetch Business & Stripe Account
                business = BusinessInfo.objects.get(pk=business_id)
                logger.info(f"Business Name: {business.businessName}")
                
                if not business.stripe_account_id:
                    logger.warning(f"Business {business_id} has no Stripe account. Skipping.")
                    # Compute pending amount/count and send "connect Stripe" reminder at most once every 3 days
                    _maybe_send_payout_connect_reminder(business)
                    continue

                logger.info(f"Stripe Account ID: {business.stripe_account_id}")

                # LOCKING: Select relevant bookings for update to prevent double-processing
                bookings_to_process = Booking.objects.select_for_update(skip_locked=True).filter(
                    schedule_instance__schedule__option__classId__businessId=business,
                    status__in=["completed", "forfeited"],
                    payment_status="paid",
                    payout_status="pending",
                    booking_date__lt=CUTOVER_DATE,
                ).exclude(payout_status="settled")
                
                booking_count_locked = bookings_to_process.count()
                logger.info(f"Acquired lock on {booking_count_locked} bookings")
                
                # Check if we still have bookings after locking
                if not bookings_to_process.exists():
                    logger.info(f"No bookings remaining after lock acquisition (another worker processing?). Skipping.")
                    continue

                # Prefetch payments for metadata (disclosed stripe_processing_fee per charge)
                bookings_to_process = bookings_to_process.prefetch_related("payments")

                # allocated_net_payout already includes Stripe processing fee deduction at payment time
                total_payout = Decimal("0.00")
                total_stripe_fees = Decimal("0.00")
                booking_ids = []
                zero_dollar_bookings = []

                for booking in bookings_to_process:
                    allocated = booking.allocated_net_payout or Decimal("0.00")
                    if allocated <= 0:
                        zero_dollar_bookings.append(booking.id)
                        continue
                    payment = next(
                        (p for p in booking.payments.all() if p.status == "succeeded"),
                        None,
                    )
                    if payment and payment.stripe_processing_fee:
                        total_stripe_fees += payment.stripe_processing_fee
                    total_payout += allocated
                    booking_ids.append(booking.id)

                logger.info(f"Total payout amount calculated: ${total_payout}")
                logger.info(f"Bookings to pay out: {len(booking_ids)}")
                logger.info(f"Zero-dollar bookings to mark processed: {len(zero_dollar_bookings)}")

                # Mark zero-dollar bookings as processed
                if zero_dollar_bookings:
                    Booking.objects.filter(id__in=zero_dollar_bookings).update(payout_status="processed")
                    logger.info(f"Marked {len(zero_dollar_bookings)} zero-dollar bookings as processed")

                if total_payout < Decimal("0.50"): # Stripe minimum varies, 0.50 is safe for CAD/USD
                    # Do NOT mark as processed. Just skip. 
                    # They will be picked up in the next run and aggregated with new bookings.
                    logger.info(f"Skipping payout for Business {business.businessId}: Amount ${total_payout} below minimum threshold.")
                    continue

                # Create Payout Record
                payout_amount_cents = int(total_payout * 100)
                idempotency_key = _build_payout_batch_idempotency_key(
                    business_id=business.businessId,
                    currency=business.currency,
                    booking_ids=booking_ids,
                    payout_amount_cents=payout_amount_cents,
                )

                existing_payout = _get_completed_payout_for_batch(idempotency_key)
                if existing_payout:
                    reconciled = _reconcile_pending_bookings_to_existing_payout(
                        existing_payout, booking_ids
                    )
                    if reconciled:
                        successful_payouts += 1
                        logger.info(
                            "✓ SUCCESS: Reconciled %s booking(s) to existing payout %s "
                            "for Business %s",
                            reconciled,
                            existing_payout.stripe_transfer_id,
                            business.businessId,
                        )
                    else:
                        logger.info(
                            "Batch %s already paid via payout %s; no pending bookings to reconcile.",
                            idempotency_key,
                            existing_payout.stripe_transfer_id,
                        )
                    continue

                temp_transfer_id = (
                    f"temp_task_{business.businessId}_{idempotency_key.split(':')[-1]}"
                )

                logger.info(f"Creating payout record with temp ID: {temp_transfer_id}")

                payout_record = Payout.objects.create(
                    business=business,
                    stripe_transfer_id=temp_transfer_id,
                    amount=total_payout,
                    currency=business.currency.upper(),
                    arrival_date=timezone.now().date() + timedelta(days=3),
                    status="pending",
                    metadata={
                        "source": "daily_payout_task",
                        "business_id": business.businessId,
                        "booking_count": len(booking_ids),
                        "stripe_fees_deducted": str(total_stripe_fees),
                        "batch_idempotency_key": idempotency_key,
                        "temp_id": True,
                    },
                )

                logger.info(f"Payout record created: ID={payout_record.id}")
                logger.info(f"Initiating Stripe Transfer: Amount={payout_amount_cents} cents, Currency={business.currency.lower()}")

                transfer = _create_stripe_transfer_for_batch(
                    business=business,
                    payout_amount_cents=payout_amount_cents,
                    idempotency_key=idempotency_key,
                    booking_count=len(booking_ids),
                )

                logger.info(f"Stripe Transfer successful: ID={transfer.id}")

                _finalize_successful_payout(payout_record, transfer, booking_ids)

                logger.info(f"Updated payout record with Stripe Transfer ID: {transfer.id}")
                logger.info(f"Marked {len(booking_ids)} bookings as payout_status='processed'")
                logger.info(f"Linked {len(booking_ids)} bookings to payout record")
                
                successful_payouts += 1
                logger.info(f"✓ SUCCESS: Payout {transfer.id} for Business {business.businessId}: ${total_payout}")

        except stripe.StripeError as e:
            failed_payouts += 1
            logger.error(f"✗ STRIPE ERROR for Business {business_id}: {e}")
            try:
                business = BusinessInfo.objects.filter(pk=business_id).first()
                if business and _is_business_connect_not_ready_for_payout(business, e):
                    try:
                        _send_payout_connect_required_immediate(business)
                    except Exception as biz_email_err:
                        logger.warning(
                            "Could not send business payout connect email after transfer failure: %s",
                            biz_email_err,
                        )
                else:
                    from quickstart.utils.email_utils import send_super_admin_payout_failed_email

                    send_super_admin_payout_failed_email(
                        business_id, str(e), business=business, stripe_error=True
                    )
            except Exception as email_err:
                logger.warning(
                    "Could not send payout failure notification: %s", email_err
                )
            # Note: Transaction rollback will occur automatically, keeping bookings as 'pending'

        except Exception as e:
            failed_payouts += 1
            logger.error(f"✗ ERROR processing payout for Business {business_id}: {e}", exc_info=True)
            try:
                business = BusinessInfo.objects.filter(pk=business_id).first()
                from quickstart.utils.email_utils import send_super_admin_payout_failed_email

                send_super_admin_payout_failed_email(
                    business_id, str(e), business=business, stripe_error=False
                )
            except Exception as email_err:
                logger.warning(
                    "Could not send Super Admin payout failure email: %s", email_err
                )

    logger.info("=" * 80)
    logger.info(f"TASK END: process_daily_payouts")
    logger.info(f"Summary: Successful={successful_payouts}, Failed={failed_payouts}")
    logger.info("=" * 80)
    
    return f"Payout process finished. Successful: {successful_payouts}. Failed: {failed_payouts}."

@shared_task
def process_daily_refunds():
    logger.info("=" * 80)
    logger.info("TASK START: process_daily_refunds")
    logger.info("=" * 80)

    bookings_to_refund = Booking.objects.filter(payment_status="refund_pending")

    if not bookings_to_refund.exists():
        logger.info("No refunds to process.")
        return "No refunds to process."

    successful_refunds, failed_refunds = 0, 0

    for idx, booking in enumerate(bookings_to_refund, 1):
        try:
            with transaction.atomic():
                locked_booking = Booking.objects.select_for_update().get(id=booking.id)
                
                if locked_booking.payment_status != "refund_pending":
                    continue

                # 1. Calculate Total Refund Amount based on Policy
                base_value = locked_booking.amount_paid
                refund_percentage = (Decimal(locked_booking.cancellation_refund_percentage) / 100)
                total_refund_needed = (base_value * refund_percentage).quantize(Decimal("0.01"))
                
                remaining_refund_needed = total_refund_needed
                logger.info(f"Booking {booking.id}: Total refund calculated: ${total_refund_needed}")

                # 2. Handle Gift Card Refunds FIRST
                # Find GC transactions used for THIS booking (redemptions are negative amounts)
                gc_transactions = locked_booking.gift_card_transactions.filter(
                    transaction_type='redemption'
                ).select_related('gift_card')

                for txn in gc_transactions:
                    if remaining_refund_needed <= 0:
                        break
                    
                    # The amount used is negative, so flip it
                    amount_used_from_card = abs(txn.amount)
                    
                    # Calculate how much to restore to this card
                    # We can't refund more to the card than was taken from it
                    refund_to_card = min(amount_used_from_card, remaining_refund_needed)
                    
                    if refund_to_card > 0:
                        gift_card = txn.gift_card
                        # Restore Balance
                        gift_card.current_balance += refund_to_card
                        gift_card.save()
                        
                        # Create Refund Transaction Log
                        from quickstart.models import GiftCardTransaction # Delayed import
                        GiftCardTransaction.objects.create(
                            gift_card=gift_card,
                            booking=locked_booking,
                            amount=refund_to_card,
                            balance_after=gift_card.current_balance,
                            transaction_type='refund'
                        )
                        
                        remaining_refund_needed -= refund_to_card
                        logger.info(f"Refunded ${refund_to_card} to Gift Card {gift_card.code}")

                # 3. Handle Stripe Refunds (If money is still owed)
                if remaining_refund_needed > 0:
                    payment = locked_booking.payments.filter(
                        status__in=["succeeded", "partially_refunded"]
                    ).first()

                    if payment and payment.available_refund_amount > 0:
                        # Cap refund at what is available in Stripe
                        refund_to_stripe = min(remaining_refund_needed, payment.available_refund_amount)
                        
                        if refund_to_stripe >= Decimal("0.50"):
                            create_stripe_refund(
                                payment_intent=payment.stripe_payment_intent_id,
                                amount=int(refund_to_stripe * 100),
                                reason="requested_by_customer",
                            )
                            
                            payment.refunded_amount += refund_to_stripe
                            if payment.available_refund_amount <= Decimal("0.00"):
                                payment.status = "refunded"
                            else:
                                payment.status = "partially_refunded"
                            payment.save()
                            
                            remaining_refund_needed -= refund_to_stripe
                            logger.info(f"Refunded ${refund_to_stripe} to Stripe PI {payment.stripe_payment_intent_id}")
                        else:
                            logger.info(f"Remaining Stripe refund ${refund_to_stripe} too small to process. Forfeited.")
                    else:
                        logger.warning(f"Booking {booking.id}: Need to refund ${remaining_refund_needed} but no Stripe funds available.")

                # 4. Finalize Booking Status
                if remaining_refund_needed > 0 and remaining_refund_needed < Decimal("0.50"):
                     # We processed everything possible, small dust remaining is ignored
                     locked_booking.payment_status = "refunded"
                elif remaining_refund_needed > 0:
                     # We couldn't refund everything (e.g., Stripe limit reached logic error)
                     locked_booking.payment_status = "refund_failed"
                     logger.error(f"Booking {booking.id}: Could not fully refund. Short by ${remaining_refund_needed}")
                else:
                     locked_booking.payment_status = "refunded"

                locked_booking.save(update_fields=["payment_status"])
                successful_refunds += 1

                # Alert and notify guest when refund failed
                if locked_booking.payment_status == "refund_failed":
                    _on_cancellation_refund_failed(
                        locked_booking,
                        f"Could not fully refund (short by ${remaining_refund_needed}). Manual review required.",
                    )

        except Exception as e:
            logger.error(f"✗ ERROR refunding Booking {booking.id}: {e}", exc_info=True)
            failed_refunds += 1
            _on_cancellation_refund_failed(booking, str(e))

    return f"Refunds processed. Success: {successful_refunds}, Failed: {failed_refunds}"