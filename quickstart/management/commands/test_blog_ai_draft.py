"""
Test the weekly blog AI draft pipeline. Runs the same logic as the Celery task
in-process and reports success or failure. Use this to verify the task fails
safely (no uncaught exceptions) and that drafts are created when conditions
are met.

Usage:
  python manage.py test_blog_ai_draft           # Run once, exit 0 if draft created else 1
  python manage.py test_blog_ai_draft --force-fail  # Simulate an error to verify safe failure
"""
from django.core.management.base import BaseCommand

from quickstart.tasks.business_tasks import generate_weekly_blog_draft_task


class Command(BaseCommand):
    help = (
        "Run the weekly blog draft task once (in-process). "
        "Exits 0 if a draft was created, 1 if skipped or failed. "
        "Use --force-fail to simulate an error and verify the task fails safely."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-fail",
            action="store_true",
            help="Force an exception inside the task to verify it is caught and returned (safe failure).",
        )

    def handle(self, *args, **options):
        if options["force_fail"]:
            self._run_force_fail_test()
            return

        self.stdout.write("Running weekly blog draft task (in-process)...")
        result = generate_weekly_blog_draft_task()
        self.stdout.write(f"Result: {result}")

        if result.startswith("Created draft"):
            self.stdout.write(self.style.SUCCESS("OK: Draft created. Task completed without raising."))
            return
        if result.startswith("Failed (safe)"):
            self.stdout.write(self.style.ERROR("Task failed but did not crash (exception was caught)."))
            self.stderr.write(result)
            raise SystemExit(1)
        # Skipped for any reason
        self.stdout.write(self.style.WARNING("Skipped (no draft created)."))
        raise SystemExit(1)

    def _run_force_fail_test(self):
        """Simulate an error during the task to verify it is caught and returned."""
        from unittest.mock import patch

        self.stdout.write("Simulating failure inside the task...")
        with patch(
            "quickstart.tasks.business_tasks._pick_topic_for_weekly_blog",
            side_effect=RuntimeError("Simulated failure for test"),
        ):
            result = generate_weekly_blog_draft_task()

        if result.startswith("Failed (safe)"):
            self.stdout.write(
                self.style.SUCCESS(
                    "OK: Task caught the exception and returned a failure message (no crash)."
                )
            )
            self.stdout.write(f"Returned: {result}")
        else:
            self.stdout.write(
                self.style.ERROR(
                    f"Expected 'Failed (safe): ...' but got: {result!r}. Task may have swallowed the error incorrectly."
                )
            )
            raise SystemExit(1)
