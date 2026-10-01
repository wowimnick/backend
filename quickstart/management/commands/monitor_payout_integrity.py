from django.core.management.base import BaseCommand

from quickstart.tasks.payout_tasks import (
    collect_payout_integrity_findings,
    monitor_payout_integrity,
    send_daily_payout_integrity_warning_digest,
)


class Command(BaseCommand):
    help = (
        "Run payout integrity checks. Sends alert email only when PAYOUT_SEND_EMAILS "
        "is enabled and PAYOUT_INTEGRITY_ALERT_RECIPIENTS is set."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=3,
            help="Lookback window in days for payout checks (default: 3).",
        )
        parser.add_argument(
            "--stripe-limit",
            type=int,
            default=200,
            help="Max Stripe transfers to inspect (default: 200).",
        )
        parser.add_argument(
            "--no-email",
            action="store_true",
            help="Only print findings; do not send alert email.",
        )
        parser.add_argument(
            "--include-warnings",
            action="store_true",
            help="When sending email, include warning digest mode (not just critical alerts).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        stripe_limit = options["stripe_limit"]
        no_email = options["no_email"]
        include_warnings = options["include_warnings"]

        if no_email:
            report = collect_payout_integrity_findings(
                days=days, stripe_limit=stripe_limit, include_stripe=True
            )
            critical_findings = report.get("critical_findings") or []
            warning_findings = report.get("warning_findings") or []
            self.stdout.write("=" * 80)
            self.stdout.write("PAYOUT INTEGRITY CHECK (no email)")
            self.stdout.write(f"Ran at: {report.get('ran_at')}")
            self.stdout.write(f"Window: {report.get('days')} day(s)")
            self.stdout.write(f"Stripe limit: {report.get('stripe_limit')}")
            self.stdout.write("-" * 80)
            if critical_findings:
                self.stdout.write(self.style.ERROR("Critical findings:"))
                for item in critical_findings:
                    self.stdout.write(f"  - {item}")
            if warning_findings:
                self.stdout.write(self.style.WARNING("Warning findings:"))
                for item in warning_findings:
                    self.stdout.write(f"  - {item}")
            if not critical_findings and not warning_findings:
                self.stdout.write(self.style.SUCCESS("No findings detected."))
            self.stdout.write("=" * 80)
            return

        if include_warnings:
            result = send_daily_payout_integrity_warning_digest(
                days=days, stripe_limit=stripe_limit
            )
        else:
            result = monitor_payout_integrity(days=days, stripe_limit=stripe_limit)
        self.stdout.write(str(result))
