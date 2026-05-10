"""Blocking Typesense bootstrap for web container startup (see entrypoint.sh)."""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "If Typesense auto-bootstrap is enabled and the index is missing or empty, "
        "run a full reindex before serving traffic (waits on Redis lock if another task builds)."
    )

    def handle(self, *args, **options):
        from quickstart.services.search_index_service import sync_typesense_bootstrap_at_web_startup

        sync_typesense_bootstrap_at_web_startup()

