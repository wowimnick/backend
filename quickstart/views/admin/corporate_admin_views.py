"""Super-admin API for corporate inquiries, shortlists, options, bookings."""

import logging
import os

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from quickstart.models import (
    ClassImage,
    ClassesMain,
    CorporateBooking,
    CorporateInquiry,
    CorporateShortlist,
    CorporateShortlistOption,
)
from quickstart.serializers.admin.corporate_admin_serializers import (
    AddOptionFromClassSerializer,
    CorporateBookingAdminSerializer,
    CorporateInquiryAdminSerializer,
    CorporateInquiryListSerializer,
    CorporateShortlistAdminSerializer,
    CorporateShortlistOptionReadSerializer,
    CorporateShortlistOptionWriteSerializer,
    IssueInvoiceSerializer,
)
from quickstart.services.corporate_billing import (
    create_balance_invoice,
    refund_deposit,
)
from quickstart.utils.corporate_events import log_corporate_booking_event
from quickstart.utils.permissions import CanAccessCorporateAdmin, IsAuthenticated
from quickstart.utils.admin_pagination import AdminStandardPagination
from quickstart.utils.url_utils import build_cloudfront_url

logger = logging.getLogger(__name__)


def _cover_url_for_class(cls: ClassesMain) -> str:
    img = (
        ClassImage.objects.filter(classId=cls, isCover=True).first()
        or ClassImage.objects.filter(classId=cls).first()
    )
    if not img or not img.image or not img.image.name:
        return ""
    original_path = img.image.name
    if not original_path.startswith("originals/") or not getattr(
        settings, "CLOUDFRONT_DOMAIN", None
    ):
        return ""
    base_path, _ = os.path.splitext(original_path)
    final_path = base_path.replace("originals/", "public/medium/", 1) + ".webp"
    return build_cloudfront_url(final_path) or ""


class AdminCorporateInquiryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List / retrieve corporate inquiries with optional filters.
    """

    permission_classes = [IsAuthenticated, CanAccessCorporateAdmin]
    pagination_class = AdminStandardPagination
    queryset = CorporateInquiry.objects.all().order_by("-created_at")
    serializer_class = CorporateInquiryListSerializer

    def get_queryset(self):
        qs = CorporateInquiry.objects.select_related("shortlist").order_by("-created_at")
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(
                Q(company_name__icontains=q)
                | Q(contact_name__icontains=q)
                | Q(email__icontains=q)
            )
        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return CorporateInquiryAdminSerializer
        return CorporateInquiryListSerializer

    @action(detail=True, methods=["get", "post"], url_path="shortlist")
    def shortlist(self, request, pk=None):
        inquiry = self.get_object()
        if request.method == "GET":
            try:
                sl = CorporateShortlist.objects.prefetch_related("options").get(
                    inquiry=inquiry
                )
            except CorporateShortlist.DoesNotExist:
                return Response(None)
            return Response(CorporateShortlistAdminSerializer(sl).data)

        # POST — create draft shortlist
        if CorporateShortlist.objects.filter(inquiry=inquiry).exists():
            return Response(
                {"detail": "Shortlist already exists for this inquiry."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        intro = request.data.get("intro_message", "")
        dep = int(request.data.get("deposit_percent", 25))
        sl = CorporateShortlist.objects.create(
            inquiry=inquiry,
            intro_message=intro or "",
            deposit_percent=min(100, max(1, dep)),
            status=CorporateShortlist.STATUS_DRAFT,
        )
        return Response(
            CorporateShortlistAdminSerializer(sl).data, status=status.HTTP_201_CREATED
        )


class AdminCorporateShortlistViewSet(
    viewsets.mixins.RetrieveModelMixin,
    viewsets.mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated, CanAccessCorporateAdmin]
    serializer_class = CorporateShortlistAdminSerializer
    queryset = CorporateShortlist.objects.select_related("inquiry").prefetch_related(
        Prefetch(
            "options",
            queryset=CorporateShortlistOption.objects.select_related(
                "source_class", "source_class__location_ref"
            ).prefetch_related(
                Prefetch(
                    "source_class__images",
                    queryset=ClassImage.objects.order_by("imageId"),
                )
            ).order_by("position", "created_at"),
        ),
    )
    lookup_field = "pk"
    http_method_names = ["get", "put", "patch", "head", "options", "post"]

    def get_queryset(self):
        return CorporateShortlist.objects.select_related("inquiry").prefetch_related(
            Prefetch(
                "options",
                queryset=CorporateShortlistOption.objects.select_related(
                    "source_class", "source_class__location_ref"
                ).prefetch_related(
                    Prefetch(
                        "source_class__images",
                        queryset=ClassImage.objects.order_by("imageId"),
                    )
                ).order_by("position", "created_at"),
            ),
        )

    @action(detail=True, methods=["post"], url_path="options")
    def add_option(self, request, pk=None):
        sl = self.get_object()
        if sl.status in (
            CorporateShortlist.STATUS_CANCELLED,
        ):
            return Response(
                {"detail": "Shortlist is cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ser = CorporateShortlistOptionWriteSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        opt = CorporateShortlistOption.objects.create(shortlist=sl, **ser.validated_data)
        return Response(
            CorporateShortlistOptionReadSerializer(opt).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="options/from-class")
    def add_option_from_class(self, request, pk=None):
        sl = self.get_object()
        ser = AddOptionFromClassSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        d = ser.validated_data
        cls = d["_class"]
        cover = d.get("cover_image_url_override") or _cover_url_for_class(cls)
        title = (d.get("title_override") or "").strip() or cls.title
        desc = (d.get("description_override") or "").strip() or (cls.description or "")
        host = (d.get("host_name") or "").strip() or (
            cls.businessId.businessName if cls.businessId else ""
        )
        tagline_val = (d.get("tagline") or "").strip()[:500]
        opt = CorporateShortlistOption.objects.create(
            shortlist=sl,
            position=d["position"],
            source_type=CorporateShortlistOption.SOURCE_EXISTING_CLASS,
            source_class=cls,
            title=title[:200],
            host_name=host[:200],
            tagline=tagline_val,
            description=desc[:8000],
            inclusions=d.get("inclusions") or [],
            cover_image_url=cover,
            gallery_urls=[],
            location_text=cls.location or "",
            duration_minutes=d.get("duration_minutes"),
            min_headcount=d.get("min_headcount"),
            max_headcount=d.get("max_headcount"),
            price_total_cents=d["price_total_cents"],
            price_per_person_cents=d.get("price_per_person_cents"),
            proposed_date_options=d.get("proposed_date_options") or [],
        )
        return Response(
            CorporateShortlistOptionReadSerializer(opt).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="send")
    def send_shortlist(self, request, pk=None):
        sl = self.get_object()
        if sl.status == CorporateShortlist.STATUS_CANCELLED:
            return Response(
                {"detail": "Cannot send a cancelled shortlist."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        opts = list(sl.options.filter(is_archived=False).order_by("position"))
        n = len(opts)
        if n < 1 or n > 3:
            return Response(
                {"detail": "Add 1–3 options before sending."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        positions = sorted(o.position for o in opts)
        if positions != list(range(1, n + 1)):
            return Response(
                {"detail": "Options must have positions 1..N with no gaps."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        sl.status = CorporateShortlist.STATUS_SENT
        sl.sent_at = timezone.now()
        sl.save(update_fields=["status", "sent_at", "updated_at"])
        try:
            from quickstart.tasks.corporate_booking_tasks import (
                send_shortlist_sent_to_admins,
                send_shortlist_to_corporate,
            )

            send_shortlist_to_corporate.delay(str(sl.id))
            send_shortlist_sent_to_admins.delay(str(sl.id))
        except Exception as e:
            logger.exception("send shortlist emails: %s", e)
        return Response(CorporateShortlistAdminSerializer(sl).data)


class AdminCorporateShortlistOptionViewSet(
    viewsets.mixins.UpdateModelMixin,
    viewsets.mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated, CanAccessCorporateAdmin]
    queryset = CorporateShortlistOption.objects.select_related(
        "shortlist", "source_class", "source_class__location_ref"
    ).prefetch_related(
        Prefetch(
            "source_class__images",
            queryset=ClassImage.objects.order_by("imageId"),
        )
    )
    serializer_class = CorporateShortlistOptionWriteSerializer
    lookup_field = "pk"

    def get_serializer_class(self):
        if self.action in ("update", "partial_update"):
            return CorporateShortlistOptionWriteSerializer
        return CorporateShortlistOptionReadSerializer

    def partial_update(self, request, *args, **kwargs):
        opt = self.get_object()
        ser = CorporateShortlistOptionWriteSerializer(
            opt, data=request.data, partial=True
        )
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save()
        opt.refresh_from_db()
        return Response(CorporateShortlistOptionReadSerializer(opt).data)

    def perform_destroy(self, instance):
        instance.is_archived = True
        instance.save(update_fields=["is_archived", "updated_at"])


class AdminCorporateBookingViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, CanAccessCorporateAdmin]
    serializer_class = CorporateBookingAdminSerializer
    queryset = CorporateBooking.objects.select_related(
        "shortlist__inquiry", "selected_option"
    ).prefetch_related("events")
    lookup_field = "pk"

    def get_queryset(self):
        qs = CorporateBooking.objects.select_related(
            "shortlist__inquiry", "selected_option"
        ).prefetch_related("events")
        st = self.request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(
                Q(reference__icontains=q)
                | Q(shortlist__inquiry__company_name__icontains=q)
                | Q(billing_email__icontains=q)
            )
        return qs.order_by("-created_at")

    @action(detail=True, methods=["post"], url_path="issue-invoice")
    def issue_invoice(self, request, pk=None):
        booking = self.get_object()
        ser = IssueInvoiceSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        try:
            out = create_balance_invoice(
                booking, due_in_days=ser.validated_data.get("due_in_days", 15)
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        log_corporate_booking_event(
            booking,
            "invoice_issued",
            f"Balance invoice {out.get('invoice_id')}",
            user=request.user,
        )
        try:
            from quickstart.tasks.corporate_booking_tasks import send_invoice_issued
            send_invoice_issued.delay(str(booking.id))
        except Exception as e:
            logger.exception("invoice issued email: %s", e)
        return Response({**out, "booking": CorporateBookingAdminSerializer(booking).data})

    @action(detail=True, methods=["post"], url_path="mark-completed")
    def mark_completed(self, request, pk=None):
        booking = self.get_object()
        if booking.status not in (
            CorporateBooking.ST_FULLY_PAID,
            CorporateBooking.ST_IN_PROGRESS,
            CorporateBooking.ST_INVOICED,
            CorporateBooking.ST_DEPOSIT_PAID,
        ):
            return Response(
                {"detail": "Invalid status for completion."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        booking.status = CorporateBooking.ST_COMPLETED
        booking.save(update_fields=["status", "updated_at"])
        log_corporate_booking_event(
            booking, "completed", "Marked completed.", user=request.user
        )
        return Response(CorporateBookingAdminSerializer(booking).data)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel_booking(self, request, pk=None):
        booking = self.get_object()
        refund = request.data.get("refund_deposit", True)
        with transaction.atomic():
            if refund and booking.deposit_payment_intent_id:
                try:
                    refund_deposit(booking)
                except Exception as e:
                    return Response(
                        {"detail": f"Refund failed: {e}"},
                        status=status.HTTP_502_BAD_GATEWAY,
                    )
            booking.status = (
                CorporateBooking.ST_REFUNDED
                if refund
                else CorporateBooking.ST_CANCELLED
            )
            booking.save(update_fields=["status", "updated_at"])
            if booking.shortlist:
                booking.shortlist.status = CorporateShortlist.STATUS_CANCELLED
                booking.shortlist.save(update_fields=["status", "updated_at"])
        log_corporate_booking_event(
            booking, "cancelled", "Cancelled by admin.", user=request.user
        )
        try:
            from quickstart.tasks.corporate_booking_tasks import send_booking_cancelled
            send_booking_cancelled.delay(str(booking.id))
        except Exception as e:
            logger.exception("cancel email: %s", e)
        return Response(CorporateBookingAdminSerializer(booking).data)
