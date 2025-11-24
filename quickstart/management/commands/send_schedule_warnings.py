from django.core.management.base import BaseCommand
from quickstart.tasks.business_tasks import notify_businesses_of_expiring_schedules

class Command(BaseCommand):
    help = 'Manually sends email warnings to businesses with classes ending within 14 days or less.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--async',
            action='store_true',
            help='Dispatch task to Celery worker instead of running locally/synchronously',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Starting schedule expiry check..."))

        try:
            if options['async']:
                # Send to Celery worker
                task = notify_businesses_of_expiring_schedules.delay()
                self.stdout.write(self.style.SUCCESS(f"Task dispatched to Celery. Task ID: {task.id}"))
            else:
                # Run synchronously in the terminal
                result = notify_businesses_of_expiring_schedules()
                self.stdout.write(self.style.SUCCESS(f"Execution Complete: {result}"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"An error occurred: {str(e)}"))