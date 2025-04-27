# quickstart/apps.py
from django.apps import AppConfig
import logging # Use logging

logger = logging.getLogger(__name__) # Get a logger instance

class QuickstartConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'quickstart'

    def ready(self):
        # This method is called when Django starts.
        logger.info(f"AppConfig {self.name} ready() method executing...") # Log start
        try:
            # Attempt to import the signals module from within the same app
            import quickstart.signals
            # Explicitly log success after the import line has run without error
            logger.info(f"Successfully imported quickstart.signals in {self.name}.ready()")
        except ImportError:
            # Log a warning if the signals module cannot be imported
            logger.warning("quickstart.signals module not found or could not be imported.")
        except Exception as e:
            # Catch any other unexpected errors during import
            logger.error(f"An unexpected error occurred during signal import in {self.name}.ready(): {e}", exc_info=True)
        logger.info(f"AppConfig {self.name} ready() method finished.") # Log end