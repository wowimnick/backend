# Data migration: keep one WidgetSubscription per business (prefer active/trialing, else latest by created_at)

from django.db import migrations


def collapse_one_per_business(apps, schema_editor):
    WidgetSubscription = apps.get_model("quickstart", "WidgetSubscription")
    # Group by business_id, find those with more than one row
    from django.db.models import Count

    dupes = (
        WidgetSubscription.objects.values("business_id")
        .annotate(c=Count("id"))
        .filter(c__gt=1)
    )
    business_ids = [d["business_id"] for d in dupes]
    for bid in business_ids:
        rows = list(WidgetSubscription.objects.filter(business_id=bid))

        def sort_key(r):
            # Keep best first: active/trialing > other; then latest current_period_end; then latest created_at
            status_prio = 0 if r.status in ("active", "trialing") else 1
            period_ts = r.current_period_end.timestamp() if r.current_period_end else 0
            created_ts = r.created_at.timestamp() if r.created_at else 0
            return (status_prio, -period_ts, -created_ts)

        rows.sort(key=sort_key)
        keep = rows[0]
        to_delete = rows[1:]
        for r in to_delete:
            r.delete()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0206_first_purchase_gift_card_sent"),
    ]

    operations = [
        migrations.RunPython(collapse_one_per_business, noop),
    ]
