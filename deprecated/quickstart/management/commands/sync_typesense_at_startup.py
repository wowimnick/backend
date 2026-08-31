"""Blocking Typesense bootstrap for web container startup (see entrypoint.sh)."""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Prepare Typesense before serving traffic. With TYPESENSE_FULL_REINDEX_EACH_DEPLOY (default), "
        "runs a full reindex once per deploy build id (BUILD_ID/IMAGE_TAG/GIT_SHA); other web tasks skip. "
        "Otherwise only bootstraps when the index is missing or empty. Uses Redis lock / peer wait."
    )

    def handle(self, *args, **options):
        from quickstart.services.search_index_service import sync_typesense_at_web_deploy_startup

        sync_typesense_at_web_deploy_startup()

