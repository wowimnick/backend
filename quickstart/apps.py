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

        # Cache version is no longer bumped on web startup; marketplace public
        # cache commands were moved to deprecated/.

        logger.info(f"AppConfig {self.name} ready() method finished.")
