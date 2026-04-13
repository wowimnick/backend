# Backfill Payment.stripe_processing_fee and reduce Booking.allocated_net_payout for legacy rows only

from decimal import Decimal

from django.db import migrations


def _estimate_stripe_processing_fee(charge_amount):
    if charge_amount is None or charge_amount <= 0:
        return Decimal("0.00")
    return (charge_amount * Decimal("0.029") + Decimal("0.30")).quantize(Decimal("0.01"))


def forwards(apps, schema_editor):
    Payment = apps.get_model("quickstart", "Payment")
    Booking = apps.get_model("quickstart", "Booking")

    backfilled_payment_ids = set()
    for pay in Payment.objects.filter(status="succeeded").iterator(chunk_size=500):
        existing = pay.stripe_processing_fee or Decimal("0.00")
        if existing > 0:
            continue
        fee = _estimate_stripe_processing_fee(pay.amount)
        Payment.objects.filter(pk=pay.pk).update(stripe_processing_fee=fee)
        backfilled_payment_ids.add(pay.pk)

    def payment_for_booking(booking):
        pay = (
            Payment.objects.filter(booking_id=booking.id, status="succeeded")
            .order_by("-id")
            .first()
        )
        if pay:
            return pay
        if booking.booking_group_id:
            return (
                Payment.objects.filter(
                    booking__booking_group_id=booking.booking_group_id,
                    status="succeeded",
                )
                .order_by("-id")
                .first()
            )
        return None

    for b in (
        Booking.objects.filter(payment_status="paid")
        .exclude(allocated_net_payout__isnull=True)
        .iterator(chunk_size=500)
    ):
        pay = payment_for_booking(b)
        if not pay or pay.pk not in backfilled_payment_ids:
            continue
        if not pay.amount or pay.amount <= 0:
            continue
        fee = pay.stripe_processing_fee or Decimal("0.00")
        share = (b.amount_paid / pay.amount).quantize(Decimal("0.0001"))
        stripe_part = (fee * share).quantize(Decimal("0.01"))
        current = b.allocated_net_payout or Decimal("0.00")
        new_alloc = current - stripe_part
        if new_alloc < 0:
            new_alloc = Decimal("0.00")
        Booking.objects.filter(pk=b.pk).update(allocated_net_payout=new_alloc)


def backwards(apps, schema_editor):
    Payment = apps.get_model("quickstart", "Payment")
    Payment.objects.all().update(stripe_processing_fee=Decimal("0.00"))


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0227_payment_stripe_processing_fee"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
