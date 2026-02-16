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

        try:
            from quickstart.utils.meta_capi import log_capi_config_at_boot

            log_capi_config_at_boot()
        except Exception as e:
            logger.warning("Meta CAPI boot log skipped: %s", e)

        # Cache version is bumped only by clear_public_caches in the web entrypoint (once per build).
        # Do not bump here: ready() runs in every process (every Gunicorn worker, every scaled ECS task).
        # Bumping here would invalidate the cache on scale-out with no prewarm.

        logger.info(f"AppConfig {self.name} ready() method finished.")
