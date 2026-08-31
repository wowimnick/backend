"""Token-based public API for corporate shortlist (no auth)."""

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django.db.models import Prefetch

from quickstart.models import (
    ClassImage,
    CorporateBooking,
    CorporateInquiry,
    CorporateShortlist,
    CorporateShortlistOption,
)
from quickstart.serializers.public.corporate_shortlist_serializers import (
    CorporateSelectOptionSerializer,
    CorporateSupportMessageSerializer,
)
from quickstart.services.corporate_billing import (
    create_balance_payment_intent,
    create_or_reuse_deposit_payment_intent,
)
from quickstart.utils.corporate_events import log_corporate_booking_event
from quickstart.utils.corporate_shortlist_images import effective_gallery_urls_for_option
from quickstart.utils.corporate_shortlist_location import (
    location_label_for_option,
    maps_search_query_for_option,
)

logger = logging.getLogger(__name__)


def _opt_out(o: CorporateShortlistOption) -> dict:
    slug = ""
    sc = getattr(o, "source_class", None)
    if sc is not None:
        slug = (getattr(sc, "slug", None) or "").strip()
    return {
        "id": str(o.id),
        "position": o.position,
        "source_type": o.source_type,
        "class_slug": slug,
        "title": o.title,
        "host_name": o.host_name,
        "tagline": o.tagline,
        "description": o.description,
        "inclusions": o.inclusions or [],
        "cover_image_url": o.cover_image_url or "",
        "gallery_urls": effective_gallery_urls_for_option(o),
        "location_text": o.location_text or "",
        "location_label": location_label_for_option(o),
        "maps_query": maps_search_query_for_option(o),
        "duration_minutes": o.duration_minutes,
        "min_headcount": o.min_headcount,
        "max_headcount": o.max_headcount,
        "price_total_cents": o.price_total_cents,
        "price_per_person_cents": o.price_per_person_cents,
        "proposed_date_options": o.proposed_date_options or [],
    }


def _inquiry_out(inq: CorporateInquiry) -> dict:
    return {
        "company_name": inq.company_name,
        "contact_name": inq.contact_name,
    }


def _booking_out(b: CorporateBooking) -> dict:
    return {
        "id": str(b.id),
        "reference": b.reference,
        "status": b.status,
        "headcount": b.headcount,
        "confirmed_datetime": b.confirmed_datetime,
        "total_cents": b.total_cents,
        "deposit_cents": b.deposit_cents,
        "balance_cents": b.balance_cents,
        "currency": b.currency,
        "deposit_paid_at": b.deposit_paid_at,
        "invoice_url": b.invoice_url or "",
        "invoice_status": b.invoice_status or "",
        "invoice_due_at": b.invoice_due_at,
        "balance_paid_at": b.balance_paid_at,
        "selected_option_id": str(b.selected_option_id),
    }


def _active_booking(sl: CorporateShortlist):
    return (
        sl.bookings.exclude(
            status__in=[
                CorporateBooking.ST_CANCELLED,
                CorporateBooking.ST_REFUNDED,
            ]
        )
        .order_by("-created_at")
        .first()
    )


def _touch_shortlist_view(sl: CorporateShortlist):
    now = timezone.now()
    updates = ["last_viewed_at", "updated_at"]
    sl.last_viewed_at = now
    if sl.first_viewed_at is None:
        sl.first_viewed_at = now
        if sl.status == CorporateShortlist.STATUS_SENT:
            sl.status = CorporateShortlist.STATUS_VIEWED
            updates.append("status")
    sl.save(update_fields=updates)


def _build_payload(sl: CorporateShortlist) -> dict:
    inq = sl.inquiry
    option_qs = (
        CorporateShortlistOption.objects.filter(shortlist=sl, is_archived=False)
        .select_related("source_class", "source_class__location_ref")
        .prefetch_related(
            Prefetch(
                "source_class__images",
                queryset=ClassImage.objects.order_by("imageId"),
            )
        )
        .order_by("position", "created_at")
    )
    options = list(option_qs)
    booking = _active_booking(sl)
    return {
        "id": str(sl.id),
        "token": str(sl.token),
        "status": sl.status,
        "intro_message": sl.intro_message,
        "presentation": getattr(sl, "presentation", None) or {},
        "deposit_percent": sl.deposit_percent,
        "currency": sl.currency,
        "inquiry": _inquiry_out(inq),
        "options": [_opt_out(o) for o in options],
        "active_booking": _booking_out(booking) if booking else None,
    }


class CorporateShortlistPublicView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, token):
        try:
            sl = CorporateShortlist.objects.select_related("inquiry").get(token=token)
        except CorporateShortlist.DoesNotExist:
            return Response({"detail": "Shortlist not found."}, status=status.HTTP_404_NOT_FOUND)

        if sl.status == CorporateShortlist.STATUS_CANCELLED:
            return Response(
                {
                    "detail": "This shortlist is no longer available.",
                    "code": "cancelled",
                },
                status=status.HTTP_410_GONE,
            )

        if sl.status not in (
            CorporateShortlist.STATUS_SENT,
            CorporateShortlist.STATUS_VIEWED,
            CorporateShortlist.STATUS_ACCEPTED,
        ):
            return Response(
                {"detail": "This shortlist is not available yet."},
                status=status.HTTP_403_FORBIDDEN,
            )

        _touch_shortlist_view(sl)
        return Response(_build_payload(sl))


class CorporateShortlistSelectView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, token):
        ser = CorporateSelectOptionSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        data = ser.validated_data

        try:
            sl = CorporateShortlist.objects.select_related("inquiry").get(token=token)
        except CorporateShortlist.DoesNotExist:
            return Response({"detail": "Shortlist not found."}, status=status.HTTP_404_NOT_FOUND)

        if sl.status == CorporateShortlist.STATUS_CANCELLED:
            return Response(
                {"detail": "This shortlist is no longer available."},
                status=status.HTTP_410_GONE,
            )

        if sl.status not in (
            CorporateShortlist.STATUS_SENT,
            CorporateShortlist.STATUS_VIEWED,
        ):
            # Already accepted with booking, etc.
            if _active_booking(sl):
                return Response(
                    {"detail": "A booking is already in progress for this shortlist."},
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {"detail": "This shortlist is not open for selection."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if _active_booking(sl):
            return Response(
                {"detail": "A booking is already in progress for this shortlist."},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            opt = CorporateShortlistOption.objects.get(
                id=data["option_id"],
                shortlist=sl,
                is_archived=False,
            )
        except CorporateShortlistOption.DoesNotExist:
            return Response({"option_id": "Invalid option."}, status=status.HTTP_400_BAD_REQUEST)

        hc = int(data["headcount"])
        if opt.min_headcount and hc < opt.min_headcount:
            return Response(
                {"headcount": f"Minimum headcount is {opt.min_headcount}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if opt.max_headcount and hc > opt.max_headcount:
            return Response(
                {"headcount": f"Maximum headcount is {opt.max_headcount}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        confirmed = data["confirmed_datetime"]
        proposed = opt.proposed_date_options or []
        if proposed:

            def _matches(cdt: datetime, raw: str) -> bool:
                if raw == cdt.isoformat():
                    return True
                try:
                    pd = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    return abs((pd - cdt).total_seconds()) < 120
                except Exception:
                    return raw[:16] == cdt.isoformat()[:16]

            if not any(_matches(confirmed, p) for p in proposed):
                return Response(
                    {
                        "confirmed_datetime": "Selected time must be one of the proposed options.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        total = int(opt.price_total_cents)
        pct = int(sl.deposit_percent)
        deposit = max(1, round(total * pct / 100))
        balance = max(0, total - deposit)

        with transaction.atomic():
            booking = CorporateBooking.objects.create(
                shortlist=sl,
                selected_option=opt,
                status=CorporateBooking.ST_PENDING,
                headcount=hc,
                confirmed_datetime=confirmed,
                special_requests=data.get("special_requests") or "",
                billing_company_name=data["billing_company_name"],
                billing_contact_name=data["billing_contact_name"],
                billing_email=data["billing_email"].strip().lower(),
                billing_address=data.get("billing_address") or {},
                po_number=data.get("po_number") or "",
                total_cents=total,
                deposit_cents=deposit,
                balance_cents=balance,
                currency=sl.currency or "usd",
            )
            sl.status = CorporateShortlist.STATUS_ACCEPTED
            sl.save(update_fields=["status", "updated_at"])
            log_corporate_booking_event(
                booking,
                "option_selected",
                f"Option {opt.title} selected; {hc} guests.",
            )

        try:
            from quickstart.tasks.corporate_booking_tasks import send_option_selected_to_admins
            send_option_selected_to_admins.delay(str(booking.id))
        except Exception as e:
            logger.exception("queue option selected email: %s", e)

        return Response(
            {
                "booking": _booking_out(booking),
                "shortlist": _build_payload(
                    CorporateShortlist.objects.select_related("inquiry").get(pk=sl.pk)
                ),
            },
            status=status.HTTP_201_CREATED,
        )


class CorporateBookingDepositIntentView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, token, booking_id):
        try:
            sl = CorporateShortlist.objects.get(token=token)
        except CorporateShortlist.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            booking = CorporateBooking.objects.get(id=booking_id, shortlist=sl)
        except CorporateBooking.DoesNotExist:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

        if booking.status != CorporateBooking.ST_PENDING:
            return Response(
                {"detail": "Deposit already processed or not required."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            out = create_or_reuse_deposit_payment_intent(booking)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception("deposit intent: %s", e)
            return Response(
                {"detail": "Could not start payment. Try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(out)


class CorporateBookingBalanceIntentView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, token, booking_id):
        try:
            sl = CorporateShortlist.objects.get(token=token)
        except CorporateShortlist.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            booking = CorporateBooking.objects.get(id=booking_id, shortlist=sl)
        except CorporateBooking.DoesNotExist:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

        if booking.status not in (
            CorporateBooking.ST_DEPOSIT_PAID,
            CorporateBooking.ST_INVOICED,
        ):
            return Response(
                {"detail": "Balance payment is not available for this booking."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            out = create_balance_payment_intent(booking)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception("balance intent: %s", e)
            return Response(
                {"detail": "Could not start payment. Try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(out)


class CorporateShortlistSupportView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, token):
        ser = CorporateSupportMessageSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            sl = CorporateShortlist.objects.select_related("inquiry").get(token=token)
        except CorporateShortlist.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if sl.status == CorporateShortlist.STATUS_CANCELLED:
            return Response(
                {"detail": "This shortlist is no longer available."},
                status=status.HTTP_410_GONE,
            )

        data = ser.validated_data
        try:
            from quickstart.tasks.corporate_booking_tasks import send_corporate_support_message

            send_corporate_support_message.delay(
                str(sl.id),
                data["name"],
                data["email"],
                data["message"],
            )
        except Exception as e:
            logger.exception("queue support message: %s", e)
            return Response(
                {"detail": "Could not send your message. Try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response({"detail": "Message sent."}, status=status.HTTP_202_ACCEPTED)


class CorporateBookingStatusPublicView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, token, booking_id):
        try:
            sl = CorporateShortlist.objects.get(token=token)
        except CorporateShortlist.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            booking = CorporateBooking.objects.get(id=booking_id, shortlist=sl)
        except CorporateBooking.DoesNotExist:
            return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_booking_out(booking))
