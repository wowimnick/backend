import os
import sys
from django.apps import AppConfig
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class QuickstartConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "quickstart"
    path = os.path.dirname(os.path.abspath(__file__))

    def ready(self):
        logger.info(f"AppConfig {self.name} ready() method executing...")
        
        # Import signals first
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
        
        # ONE-TIME PAYOUT TASK - REMOVE THIS BLOCK AFTER IT RUNS SUCCESSFULLY
        # Skip during migrations, management commands, or Celery worker/beat processes
        if 'migrate' not in sys.argv and 'makemigrations' not in sys.argv and 'collectstatic' not in sys.argv and 'celery' not in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            if settings.IS_DEPLOYED_ENV:
                try:
                    from quickstart.tasks.payout_tasks import process_daily_payouts
                    # Schedule to run 60 seconds after boot to ensure Celery workers are ready
                    result = process_daily_payouts.apply_async(countdown=60)
                    logger.info(f"🚀 ONE-TIME PAYOUT TASK scheduled (Task ID: {result.id}) - will run in 60 seconds")
                    logger.warning("⚠️  REMINDER: DELETE THIS BLOCK FROM apps.py AFTER PAYOUT COMPLETES!")
                except Exception as e:
                    logger.error(f"Failed to schedule startup payout task: {e}", exc_info=True)
        # END OF ONE-TIME BLOCK - DELETE EVERYTHING ABOVE THIS LINE AFTER SUCCESS
        
        logger.info(f"AppConfig {self.name} ready() method finished.")