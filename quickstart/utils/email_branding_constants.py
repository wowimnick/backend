"""
Registry of transactional email branding HTML modes and placeholder requirements.

Used by widget_config validation and email_utils custom HTML rendering.
Frontend mirrors keys in emailBrandingPlaceholders.js (keep in sync manually).
"""

from typing import Dict, FrozenSet

# Max size per custom HTML template (bytes)
EMAIL_BRANDING_HTML_MAX_BYTES = 100_000

# Keys stored in branding JSON custom_html dict and used by the dashboard preview tabs
EMAIL_TYPE_BOOKING_CONFIRMATION = "booking_confirmation"
EMAIL_TYPE_BOOKING_REMINDER = "booking_reminder"
EMAIL_TYPE_BOOKING_CANCELLED_BY_HOST = "booking_cancelled_by_host"
EMAIL_TYPE_BOOKING_RESCHEDULED = "booking_rescheduled"
EMAIL_TYPE_BOOKING_CANCELLATION_CONFIRMED = "booking_cancellation_confirmed"

ALL_EMAIL_TYPE_KEYS = frozenset(
    {
        EMAIL_TYPE_BOOKING_CONFIRMATION,
        EMAIL_TYPE_BOOKING_REMINDER,
        EMAIL_TYPE_BOOKING_CANCELLED_BY_HOST,
        EMAIL_TYPE_BOOKING_RESCHEDULED,
        EMAIL_TYPE_BOOKING_CANCELLATION_CONFIRMED,
    }
)

# mode values in branding JSON
BRANDING_MODE_BUILDER = "builder"
BRANDING_MODE_HTML = "html"

# Each entry: required placeholders must appear as {{name}} in the HTML string
PLACEHOLDER_REQUIREMENTS: Dict[str, Dict[str, FrozenSet[str]]] = {
    EMAIL_TYPE_BOOKING_CONFIRMATION: {
        "required": frozenset(
            {
                "class_title",
                "booking_date",
                "booking_time",
                "reference_id",
                "manage_booking_url",
            }
        ),
        "optional": frozenset(
            {
                "time_range",
                "duration",
                "location",
                "business_name",
                "first_name",
                "last_name",
                "customer_email",
                "logo_url",
                "primary_color",
                "business_email",
                "business_phone",
                "cancellation_policy",
                "participants",
                "option_title",
                "equipment",
                "cancel_booking_url",
                "class_details_url",
                "footer_text",
                "confirmation_message",
            }
        ),
    },
    EMAIL_TYPE_BOOKING_REMINDER: {
        "required": frozenset(
            {
                "class_title",
                "booking_date",
                "booking_time",
                "manage_booking_url",
            }
        ),
        "optional": frozenset(
            {
                "time_range",
                "duration",
                "location",
                "business_name",
                "first_name",
                "last_name",
                "customer_email",
                "logo_url",
                "primary_color",
                "business_email",
                "business_phone",
                "cancellation_policy",
                "participants",
                "option_title",
                "equipment",
                "cancel_booking_url",
                "class_details_url",
                "footer_text",
                "confirmation_message",
            }
        ),
    },
    EMAIL_TYPE_BOOKING_CANCELLED_BY_HOST: {
        "required": frozenset(
            {
                "class_title",
                "booking_date",
                "booking_time",
            }
        ),
        "optional": frozenset(
            {
                "reason",
                "refund_info",
                "first_name",
                "last_name",
                "customer_email",
                "business_name",
                "logo_url",
                "primary_color",
                "explore_url",
                "footer_text",
            }
        ),
    },
    EMAIL_TYPE_BOOKING_RESCHEDULED: {
        "required": frozenset(
            {
                "class_title",
                "booking_date",
                "booking_time",
                "new_date",
                "new_time",
            }
        ),
        "optional": frozenset(
            {
                "time_range",
                "first_name",
                "last_name",
                "customer_email",
                "business_name",
                "logo_url",
                "primary_color",
                "manage_booking_url",
                "footer_text",
                "timezone",
            }
        ),
    },
    EMAIL_TYPE_BOOKING_CANCELLATION_CONFIRMED: {
        "required": frozenset(
            {
                "class_title",
                "booking_date",
                "booking_time",
                "reference_id",
            }
        ),
        "optional": frozenset(
            {
                "first_name",
                "last_name",
                "customer_email",
                "refund_info",
                "business_name",
                "logo_url",
                "primary_color",
                "explore_url",
                "footer_text",
            }
        ),
    },
}
