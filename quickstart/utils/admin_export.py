from rest_framework import status
from rest_framework.response import Response

ADMIN_EXPORT_MAX_ROWS = 10_000


def check_export_row_limit(queryset):
    count = queryset.count()
    if count > ADMIN_EXPORT_MAX_ROWS:
        return False, count
    return True, count


def export_row_limit_response(count):
    return Response(
        {
            "detail": (
                f"Export limited to {ADMIN_EXPORT_MAX_ROWS:,} rows. "
                f"Your filter matches {count:,} rows. Please narrow your filters."
            )
        },
        status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    )
