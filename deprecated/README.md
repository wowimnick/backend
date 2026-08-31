# Deprecated marketplace remnants

These files were disconnected from live ClassEasily (SaaS/widget) URLs, serializers, tasks, and Celery beat.

They are **not** a Django app. Do **not** add `deprecated` to `INSTALLED_APPS`. Do **not** import them from live `quickstart` or `CEBackend` code.

The tree mirrors the original `quickstart/` layout so the remnants can be recovered or deleted later:

- `quickstart/views/` — public marketplace, corporate, gift cards, verification, search
- `quickstart/serializers/` — matching serializers
- `quickstart/services/` — Typesense/search, corporate billing, scrapers
- `quickstart/utils/` — search cache, corporate shortlist, Meta CAPI, review AI
- `quickstart/tasks/` — gift card, corporate, search index, Instagram, Google reviews
- `quickstart/management/commands/` — sitemap/search/review/instagram management commands
- `templates/emails/` — marketplace/corporate/gift-card/verification email templates
- `quickstart/tests/` — tests that only covered the moved modules

Live helpers that bookings, guest messaging, and admin still needed were **copied** (not imported from here) into:

- `quickstart/serializers/booking_serializers.py`
- `quickstart/serializers/conversation_serializers.py`
- `quickstart/serializers/imported_google_review_serializers.py`
- `quickstart/utils/public_cache.py`
- `quickstart/utils/concierge_handover_tokens.py`
