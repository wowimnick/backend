import os
from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)


class QuickstartConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "quickstart"
    path = os.path.dirname(os.path.abspath(__file__))

    def ready(self):
        logger.info(f"AppConfig {self.name} ready() method executing...")
        try:
            import quickstart.signals

            logger.info(
                f"Successfully imported quickstart.signals in {self.name}.ready()"
            )
        except ImportError:
            logger.warning(
                "quickstart.signals module not found or could not be imported."
            )
        except Exception as e:
            logger.error(
                f"An unexpected error occurred during signal import in {self.name}.ready(): {e}",
                exc_info=True,
            )

        # On deploy, bump public class search cache version so Redis entries from before
        # this deploy (e.g. with old presigned URL expiry) are abandoned; next request refills with current code.
        try:
            from django.conf import settings
            from django.core.cache import cache

            if getattr(settings, "IS_DEPLOYED_ENV", False) and getattr(
                settings, "CACHE_URL", None
            ):
                from quickstart.views.public.public_class_views import (
                    PRESET_CACHE_VERSION_KEY,
                )

                version = cache.get(PRESET_CACHE_VERSION_KEY, 0) or 0
                new_version = version + 1
                cache.set(PRESET_CACHE_VERSION_KEY, new_version, timeout=None)
                logger.info(
                    "Bumped public class search cache version to %s (deploy startup).",
                    new_version,
                )
        except Exception as e:
            logger.warning(
                "Could not bump cache version on startup: %s (non-fatal).", e
            )

        logger.info(f"AppConfig {self.name} ready() method finished.")
