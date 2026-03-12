from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone


@dataclass(frozen=True)
class AdminMetricsWindow:
    start_date: date
    end_date: date
    start_dt: datetime
    end_dt_exclusive: datetime
    previous_start_date: date
    previous_end_date: date
    previous_start_dt: datetime
    previous_end_dt_exclusive: datetime
    period_days: int


def get_admin_metrics_timezone():
    tz_name = getattr(settings, "ADMIN_METRICS_TIMEZONE", None) or getattr(
        settings, "TIME_ZONE", "UTC"
    )
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return timezone.get_default_timezone()


def get_admin_metrics_local_now():
    return timezone.now().astimezone(get_admin_metrics_timezone())


def get_admin_metrics_window(
    query_params, default_days=30, logger=None
) -> AdminMetricsWindow:
    tz = get_admin_metrics_timezone()
    local_today = timezone.now().astimezone(tz).date()

    start_param = query_params.get("start_date")
    end_param = query_params.get("end_date")

    default_end = local_today
    default_start = default_end - timedelta(days=max(default_days - 1, 0))

    start_date = default_start
    end_date = default_end

    try:
        if start_param and end_param:
            start_date = datetime.strptime(start_param, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_param, "%Y-%m-%d").date()
        elif start_param:
            start_date = datetime.strptime(start_param, "%Y-%m-%d").date()
        elif end_param:
            end_date = datetime.strptime(end_param, "%Y-%m-%d").date()
            start_date = end_date - timedelta(days=max(default_days - 1, 0))
    except (TypeError, ValueError):
        if logger:
            logger.warning(
                "Invalid admin metrics date params: start='%s', end='%s'. Falling back to default %s-day window.",
                start_param,
                end_param,
                default_days,
            )
        start_date = default_start
        end_date = default_end

    if start_date > end_date:
        start_date, end_date = end_date, start_date

    period_days = (end_date - start_date).days + 1

    start_dt = datetime.combine(start_date, time.min, tzinfo=tz)
    end_dt_exclusive = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz)

    previous_end_date = start_date - timedelta(days=1)
    previous_start_date = previous_end_date - timedelta(days=period_days - 1)
    previous_start_dt = datetime.combine(previous_start_date, time.min, tzinfo=tz)
    previous_end_dt_exclusive = datetime.combine(
        start_date, time.min, tzinfo=tz
    )

    return AdminMetricsWindow(
        start_date=start_date,
        end_date=end_date,
        start_dt=start_dt,
        end_dt_exclusive=end_dt_exclusive,
        previous_start_date=previous_start_date,
        previous_end_date=previous_end_date,
        previous_start_dt=previous_start_dt,
        previous_end_dt_exclusive=previous_end_dt_exclusive,
        period_days=period_days,
    )
