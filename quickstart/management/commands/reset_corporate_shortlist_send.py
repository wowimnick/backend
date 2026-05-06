"""
Undo shortlist "sent" state so admin can click Send again.

IDE/runners often break `manage.py shell -c "..."` quoting on Windows; this avoids that.

Examples:
  python manage.py reset_corporate_shortlist_send --inquiry <uuid>
  python manage.py reset_corporate_shortlist_send --shortlist <uuid>
  python manage.py reset_corporate_shortlist_send --inquiry <uuid> --dry-run
  python manage.py reset_corporate_shortlist_send --all --dry-run
  python manage.py reset_corporate_shortlist_send --all
  python manage.py reset_corporate_shortlist_send --all --include-accepted
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from quickstart.models import CorporateShortlist


class Command(BaseCommand):
    help = (
        "Set corporate shortlist status to ready and clear sent_at "
        "(use after a mistaken send so the email can be sent again)."
    )

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument(
            "--inquiry",
            metavar="UUID",
            help="CorporateInquiry id (the row you open in admin).",
        )
        g.add_argument(
            "--shortlist",
            metavar="UUID",
            help="CorporateShortlist id.",
        )
        g.add_argument(
            "--all",
            action="store_true",
            help="Reset every shortlist that is sent or viewed (see --include-accepted).",
        )
        parser.add_argument(
            "--include-accepted",
            action="store_true",
            help="With --all, also reset accepted shortlists (can disagree with existing bookings — use carefully).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print only; do not save.",
        )

    def handle(self, *args, **options):
        inquiry_id = options["inquiry"]
        shortlist_id = options["shortlist"]
        reset_all = options["all"]
        include_accepted = options["include_accepted"]
        dry_run = options["dry_run"]

        if include_accepted and not reset_all:
            raise CommandError("--include-accepted only applies with --all.")

        if reset_all:
            statuses = [
                CorporateShortlist.STATUS_SENT,
                CorporateShortlist.STATUS_VIEWED,
            ]
            if include_accepted:
                statuses.append(CorporateShortlist.STATUS_ACCEPTED)

            qs = CorporateShortlist.objects.filter(status__in=statuses)
            breakdown = dict(
                qs.values("status").annotate(c=Count("id")).values_list("status", "c")
            )
            total = qs.count()
            self.stdout.write(f"status breakdown (matching): {breakdown}")
            self.stdout.write(f"total matching rows: {total}")
            if dry_run:
                self.stdout.write(self.style.WARNING("dry-run: no changes"))
                return
            n = qs.update(
                status=CorporateShortlist.STATUS_READY,
                sent_at=None,
                updated_at=timezone.now(),
            )
            self.stdout.write(self.style.SUCCESS(f"updated {n} row(s) to ready (sent_at cleared)."))
            return

        try:
            if inquiry_id:
                sl = CorporateShortlist.objects.get(inquiry_id=inquiry_id)
            else:
                sl = CorporateShortlist.objects.get(pk=shortlist_id)
        except CorporateShortlist.DoesNotExist:
            raise CommandError("No shortlist found for that id.") from None

        self.stdout.write(
            f"shortlist={sl.pk} inquiry={sl.inquiry_id} "
            f"status={sl.status!r} sent_at={sl.sent_at!r}"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("dry-run: no changes"))
            return

        sl.status = CorporateShortlist.STATUS_READY
        sl.sent_at = None
        sl.save(update_fields=["status", "sent_at", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"updated: status={sl.status!r} sent_at=None"))
