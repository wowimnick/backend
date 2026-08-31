# quickstart/views/business/contact_views.py
"""
Business dashboard API for contacts (CRM) with segment filters and CRUD.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DateTimeField, Exists, IntegerField, OuterRef, Q, Sum, Subquery
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from quickstart.models import Booking, BusinessInfo, Contact, CustomerMembership

logger = logging.getLogger(__name__)

# Segment keys and default high_value threshold (for "high_value" segment)
HIGH_VALUE_THRESHOLD = 500


def _get_business(user):
    """Get the business for the current user (owner or accepted staff)."""
    business = BusinessInfo.objects.filter(
        Q(owner=user) | Q(staff_members__user=user, staff_members__status="accepted")
    ).first()
    if not business:
        from rest_framework.exceptions import NotFound
        raise NotFound("You are not a member of any business.")
    return business


def _contact_to_dict(contact, extra=None):
    """Serialize Contact for API response."""
    data = {
        "id": str(contact.id),
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or "",
        "email": contact.email or "",
        "phone_number": contact.phone_number or "",
        "source": contact.source or "",
        "status": contact.status or "active",
        "tags": contact.tags or [],
        "lifetime_value": str(contact.lifetime_value or Decimal("0.00")),
        "booking_count": contact.booking_count or 0,
        "first_booking_at": contact.first_booking_at.isoformat() if contact.first_booking_at else None,
        "last_booking_at": contact.last_booking_at.isoformat() if contact.last_booking_at else None,
        "last_activity_at": contact.last_activity_at.isoformat() if contact.last_activity_at else None,
        "created_at": contact.created_at.isoformat() if contact.created_at else None,
        "updated_at": contact.updated_at.isoformat() if contact.updated_at else None,
    }
    if extra:
        data.update(extra)
    return data


def _apply_segment(queryset, business, segment):
    """Apply segment filter to Contact queryset (filter by bookings for this business)."""
    if not segment or segment == "all":
        return queryset
    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)
    ninety_days_ago = now - timedelta(days=90)

    # Bookings for this business (via schedule_instance -> schedule -> option -> classId -> businessId)
    business_bookings = Booking.objects.filter(
        schedule_instance__schedule__option__classId__businessId=business
    )

    if segment == "booked_last_30":
        return queryset.filter(
            Exists(
                business_bookings.filter(
                    contact=OuterRef("pk"),
                    booking_date__gte=thirty_days_ago,
                    status="confirmed",
                )
            )
        )
    if segment == "never_returned":
        # Exactly one confirmed booking
        subq = business_bookings.filter(contact=OuterRef("pk"), status="confirmed").values("contact").annotate(c=Count("id")).values("c")
        return queryset.annotate(_booking_count=Subquery(subq, output_field=IntegerField())).filter(_booking_count=1)
    if segment == "high_value":
        from django.db.models import DecimalField
        subq = (
            business_bookings.filter(
                contact=OuterRef("pk"),
                payment_status="paid",
            )
            .values("contact")
            .annotate(total=Sum("amount_paid"))
            .values("total")
        )
        return queryset.annotate(_total_spent=Subquery(subq, output_field=DecimalField())).filter(_total_spent__gte=Decimal(str(HIGH_VALUE_THRESHOLD)))
    if segment == "no_recent_activity":
        # Last booking older than 90 days (or no booking)
        last_booking = business_bookings.filter(contact=OuterRef("pk")).order_by("-booking_date").values("booking_date")[:1]
        subq = Subquery(last_booking, output_field=DateTimeField())
        return queryset.annotate(_last_booking=subq).filter(Q(_last_booking__lt=ninety_days_ago) | Q(_last_booking__isnull=True))
    if segment == "active_members":
        return queryset.filter(
            Exists(
                CustomerMembership.objects.filter(
                    contact=OuterRef("pk"),
                    status="active",
                )
            )
        )
    if segment == "lapsed_members":
        return queryset.filter(
            Exists(
                CustomerMembership.objects.filter(
                    contact=OuterRef("pk"),
                    status="canceled",
                )
            )
        )
    if segment == "leads":
        return queryset.filter(status="lead").filter(
            ~Exists(business_bookings.filter(contact=OuterRef("pk"), status="confirmed"))
        )
    return queryset


class ContactListView(APIView):
    """GET: List contacts with optional segment, search, pagination. POST: Create a contact."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from quickstart.utils.permissions import CanViewBusinessStudents
        if not (request.user.has_perm("quickstart.view_business_students") or request.user.has_perm("quickstart.access_business_dashboard")):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        business = _get_business(request.user)
        segment = (request.query_params.get("segment") or "").strip().lower()
        search = (request.query_params.get("search") or "").strip()
        page = int(request.query_params.get("page", 1) or 1)
        page_size = min(int(request.query_params.get("page_size", 20) or 20), 100)

        qs = Contact.objects.filter(business=business).order_by("last_name", "first_name")
        qs = _apply_segment(qs, business, segment or "all")

        if search:
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
                | Q(phone_number__icontains=search)
            )
        total = qs.count()
        offset = (page - 1) * page_size
        items = list(qs[offset : offset + page_size])
        return Response(
            {
                "results": [_contact_to_dict(c) for c in items],
                "count": total,
                "page": page,
                "page_size": page_size,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        if not (request.user.has_perm("quickstart.view_business_students") or request.user.has_perm("quickstart.manage_own_business_profile")):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        business = _get_business(request.user)
        data = request.data
        first_name = (data.get("first_name") or "").strip()
        last_name = (data.get("last_name") or "").strip()
        email = (data.get("email") or "").strip() or None
        if not first_name and not last_name and not email:
            return Response(
                {"error": "At least one of first_name, last_name, or email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        contact = Contact.objects.create(
            business=business,
            first_name=first_name or "",
            last_name=last_name or "",
            email=email,
            phone_number=(data.get("phone_number") or "").strip()[:100],
            source=data.get("source") or "manual_entry",
            status=data.get("status") or "active",
            tags=data.get("tags") if isinstance(data.get("tags"), list) else [],
        )
        return Response(_contact_to_dict(contact), status=status.HTTP_201_CREATED)


class ContactDetailView(APIView):
    """GET, PATCH, DELETE a single contact."""

    permission_classes = [IsAuthenticated]

    def _get_contact(self, request, contact_id):
        business = _get_business(request.user)
        return get_object_or_404(Contact, id=contact_id, business=business)

    def get(self, request, contact_id):
        if not request.user.has_perm("quickstart.view_business_students"):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        contact = self._get_contact(request, contact_id)
        return Response(_contact_to_dict(contact), status=status.HTTP_200_OK)

    def patch(self, request, contact_id):
        if not request.user.has_perm("quickstart.view_business_students"):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        contact = self._get_contact(request, contact_id)
        data = request.data
        if "first_name" in data:
            contact.first_name = (data["first_name"] or "").strip()
        if "last_name" in data:
            contact.last_name = (data["last_name"] or "").strip()
        if "email" in data:
            contact.email = (data["email"] or "").strip() or None
        if "phone_number" in data:
            contact.phone_number = (data["phone_number"] or "").strip()[:100]
        if "source" in data:
            contact.source = (data["source"] or "").strip()[:50]
        if "status" in data:
            contact.status = (data["status"] or "active").strip()[:50]
        if "tags" in data and isinstance(data["tags"], list):
            contact.tags = data["tags"]
        contact.save()
        return Response(_contact_to_dict(contact), status=status.HTTP_200_OK)

    def delete(self, request, contact_id):
        if not request.user.has_perm("quickstart.view_business_students"):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        contact = self._get_contact(request, contact_id)
        contact.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ContactTimelineView(APIView):
    """Unified client timeline: bookings, payments, memberships, notes, messages."""

    permission_classes = [IsAuthenticated]

    def get(self, request, contact_id):
        if not request.user.has_perm("quickstart.view_business_students"):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        business = _get_business(request.user)
        contact = get_object_or_404(Contact, id=contact_id, business=business)
        events = []

        bookings = (
            Booking.objects.filter(contact=contact)
            .select_related("schedule_instance__schedule__option__classId")
            .prefetch_related("payments")
            .order_by("-booking_date")[:50]
        )
        for booking in bookings:
            title = ""
            try:
                title = booking.schedule_instance.schedule.option.classId.title
            except Exception:
                title = "Booking"
            events.append(
                {
                    "type": "booking",
                    "at": booking.booking_date.isoformat() if booking.booking_date else None,
                    "title": title,
                    "status": booking.status,
                    "amount": str(booking.amount_paid or Decimal("0.00")),
                    "id": booking.id,
                }
            )
            for payment in booking.payments.all():
                events.append(
                    {
                        "type": "payment",
                        "at": payment.created_at.isoformat() if getattr(payment, "created_at", None) else None,
                        "title": "Payment",
                        "status": payment.status,
                        "amount": str(payment.amount or Decimal("0.00")),
                        "id": str(payment.id),
                    }
                )

        memberships = CustomerMembership.objects.filter(contact=contact).select_related("product")
        for membership in memberships:
            events.append(
                {
                    "type": "membership",
                    "at": membership.created_at.isoformat() if getattr(membership, "created_at", None) else None,
                    "title": getattr(membership.product, "name", "Membership"),
                    "status": membership.status,
                    "id": str(membership.id),
                }
            )

        from django.contrib.contenttypes.models import ContentType
        from quickstart.models import StudentNote

        ct = ContentType.objects.get_for_model(Contact)
        for note in StudentNote.objects.filter(content_type=ct, object_id=contact.pk).order_by("-created_at")[:50]:
            events.append(
                {
                    "type": "note",
                    "at": note.created_at.isoformat() if note.created_at else None,
                    "title": (getattr(note, "content", None) or "")[:140],
                    "id": str(note.id),
                }
            )

        events = [e for e in events if e.get("at")]
        events.sort(key=lambda e: e["at"], reverse=True)
        return Response(
            {
                "contact": _contact_to_dict(contact),
                "events": events[:100],
            }
        )


class ClientSegmentListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        business = _get_business(request.user)
        from quickstart.models import ClientSegment

        rows = ClientSegment.objects.filter(business=business)
        return Response(
            [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "definition": row.definition or {},
                    "is_builtin": row.is_builtin,
                }
                for row in rows
            ]
        )

    def post(self, request):
        business = _get_business(request.user)
        from quickstart.models import ClientSegment

        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "name is required"}, status=status.HTTP_400_BAD_REQUEST)
        row = ClientSegment.objects.create(
            business=business,
            name=name[:120],
            definition=request.data.get("definition") or {},
        )
        return Response(
            {"id": str(row.id), "name": row.name, "definition": row.definition, "is_builtin": False},
            status=status.HTTP_201_CREATED,
        )


class ClientSegmentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, segment_id):
        business = _get_business(request.user)
        from quickstart.models import ClientSegment

        row = get_object_or_404(ClientSegment, id=segment_id, business=business)
        if "name" in request.data:
            row.name = (request.data.get("name") or row.name)[:120]
        if "definition" in request.data and isinstance(request.data.get("definition"), dict):
            row.definition = request.data["definition"]
        row.save()
        return Response(
            {"id": str(row.id), "name": row.name, "definition": row.definition, "is_builtin": row.is_builtin}
        )

    def delete(self, request, segment_id):
        business = _get_business(request.user)
        from quickstart.models import ClientSegment

        row = get_object_or_404(ClientSegment, id=segment_id, business=business)
        row.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
