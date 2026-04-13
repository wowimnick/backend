# quickstart/views/business/revenue_analytics_views.py

from decimal import Decimal
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from django.db.models import (
    Sum,
    Count,
    F,
    ExpressionWrapper,
    FloatField,
    DecimalField,
    Q,
    Value,
    Case,
    When,
    IntegerField,
    Subquery,
    OuterRef,
    Min,
    DateTimeField,
    DateField,
    CharField,
    Exists,
)
from django.db.models.functions import (
    TruncDate,
    ExtractMonth,
    ExtractYear,
    Coalesce,
    TruncHour,
)
from django.utils import timezone
from datetime import datetime, timedelta, date as datetime_date
from rest_framework.exceptions import ValidationError, PermissionDenied
import csv
from django.http import HttpResponse
import logging
import pytz

from quickstart.models import (
    Booking,
    BusinessInfo,
    ClassOption,
    ClassesMain,
    CustomUser,
    MembershipPayment,
    PartnerTier,
    Payment,
)
from quickstart.utils.permissions import IsBusinessMember
from quickstart.utils.widget_booking_source import (
    WIDGET_BOOKING_SOURCES,
    business_has_growth_or_advanced_widget_plan,
    is_widget_booking_source,
)

logger = logging.getLogger(__name__)

_WIDGET_SOURCE_LIST = list(WIDGET_BOOKING_SOURCES)


class RevenueAnalyticsView(views.APIView):
    permission_classes = [IsAuthenticated, IsBusinessMember]

    @staticmethod
    def _redact_widget_breakdown_for_basic_plan(class_revenue, revenue_by_booking_type):
        """
        Basic plan: hide widget vs platform split in combined (source=all) views.
        Totals stay the same; widget amounts roll into platform for display only.
        """
        redacted_classes = []
        for row in class_revenue:
            r = dict(row)
            w = float(r.get("widget_revenue") or 0)
            if w:
                r["platform_revenue"] = float(r.get("platform_revenue") or 0) + w
                r["widget_revenue"] = 0.0
            redacted_classes.append(r)

        widget_extra = 0.0
        rest = []
        for entry in revenue_by_booking_type:
            if entry.get("name") == "Widget Booking":
                widget_extra += float(entry.get("value") or 0)
            else:
                rest.append(dict(entry))
        if widget_extra > 0:
            merged = False
            for e in rest:
                if e.get("name") == "Platform Booking":
                    e["value"] = float(e.get("value") or 0) + widget_extra
                    merged = True
                    break
            if not merged:
                rest.append({"name": "Platform Booking", "value": widget_extra})
            rest.sort(key=lambda x: -float(x.get("value") or 0))
        return redacted_classes, rest

    def get_business(self, user):
        business = (
            BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            )
            .select_related("partner_tier")
            .first()
        )  # Use select_related for efficiency
        return business

    def get_date_range(self, request):
        start_date_str = request.query_params.get("start_date")
        end_date_str = request.query_params.get("end_date")
        try:
            if start_date_str and end_date_str:
                start_date_naive = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                end_date_naive = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            else:
                end_date_naive = timezone.localdate()
                start_date_naive = end_date_naive - timedelta(days=29)
            if start_date_naive > end_date_naive:
                raise ValidationError("Start date cannot be after end date.")
            start_datetime_aware = timezone.make_aware(
                datetime.combine(start_date_naive, datetime.min.time()), pytz.utc
            )
            end_datetime_aware = timezone.make_aware(
                datetime.combine(end_date_naive, datetime.max.time()), pytz.utc
            )
            return start_datetime_aware, end_datetime_aware
        except ValueError:
            raise ValidationError("Invalid date format. Please use YYYY-MM-DD.")
        except Exception as e:
            logger.error(f"Error processing date range: {e}", exc_info=True)
            raise ValidationError("Error processing date range.")

    def get_valid_bookings_queryset(
            self, business, start_date, end_date, class_id=None, source="all"
        ):
            """
            Returns all bookings that contribute to revenue (paid).
            UPDATED: Now includes ALL bookings for a course, because revenue is split 1/N
            across them. We no longer filter for just the first booking.
            `source` can be "all" (default), "widget", or "marketplace".
            """
            if not business:
                return Booking.objects.none()

            # We include status='forfeited' implicitly because they have payment_status='paid'.
            # We exclude status='cancelled' (refunded) because they have payment_status='refunded'.
            base_bookings = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business,
                booking_date__range=[start_date, end_date],
                payment_status="paid",  # This captures 'confirmed', 'completed' and 'forfeited'
            ).select_related(
                "schedule_instance__schedule__option__classId",
                "schedule_instance__schedule__option",
                "user",
            )

            if class_id:
                base_bookings = base_bookings.filter(
                    schedule_instance__schedule__option__classId_id=class_id
                )

            _widget_payment_exists = Payment.objects.filter(
                booking=OuterRef("pk"),
                status="succeeded",
                metadata__original_stripe_metadata__booking_source__in=_WIDGET_SOURCE_LIST,
            )
            if source == "widget":
                base_bookings = base_bookings.filter(Exists(_widget_payment_exists))
            elif source == "marketplace":
                base_bookings = base_bookings.exclude(Exists(_widget_payment_exists))

            return base_bookings

    def _get_fee_rate_for_business(self, business):
        """Helper to get the fee rate from the business's tier with fallbacks."""
        if business and business.partner_tier:
            return business.partner_tier.fee_percentage / Decimal("100.0")

        try:
            default_tier = PartnerTier.objects.get(is_default=True)
            if business:
                logger.warning(
                    f"Business {business.businessId} was missing a partner tier. Fell back to default tier '{default_tier.name}'."
                )
            return default_tier.fee_percentage / Decimal("100.0")
        except PartnerTier.DoesNotExist:
            logger.error(
                "CRITICAL: No default PartnerTier is configured. Using hardcoded 13% fee."
            )
            return Decimal("0.13")

    def _widget_fee_rate_decimal(self, plan_id):
        key = (plan_id or "basic").lower()
        pct = {
            "basic": Decimal("4"),
            "growth": Decimal("3"),
            "advanced": Decimal("2"),
        }.get(key, Decimal("4"))
        return pct / Decimal("100.0")

    def _stripe_meta_dict(self, meta):
        if not meta or not isinstance(meta, dict):
            return {}
        inner = meta.get("original_stripe_metadata")
        if isinstance(inner, dict):
            return inner
        return meta

    def _platform_fee_rate_for_booking(self, booking, business):
        pay = None
        for p in booking.payments.all():
            if p.status == "succeeded":
                pay = p
                break
        if pay:
            src = self._stripe_meta_dict(pay.metadata)
            if is_widget_booking_source(src.get("booking_source")):
                return self._widget_fee_rate_decimal(src.get("plan_id"))
        return self._get_fee_rate_for_business(business)

    def _gift_card_amount_from_payment(self, pay, booking_amount_paid):
        if not pay or not pay.metadata:
            return Decimal("0.00")
        src = self._stripe_meta_dict(pay.metadata)
        raw = src.get("gift_card_amount_to_deduct")
        if not raw and not src.get("paid_via_giftcard"):
            return Decimal("0.00")
        try:
            gc = Decimal(str(raw or "0"))
        except Exception:
            gc = Decimal("0.00")
        if pay.amount and pay.amount > 0 and booking_amount_paid:
            return (gc * (booking_amount_paid / pay.amount)).quantize(Decimal("0.01"))
        return gc.quantize(Decimal("0.01"))

    def _get_membership_payment_aggregates(self, business, start_date, end_date):
        """Aggregate MembershipPayment for the business in the date range (status=paid)."""
        qs = MembershipPayment.objects.filter(
            membership__product__business=business,
            status="paid",
            created_at__range=[start_date, end_date],
        )
        return qs.aggregate(
            total_amount=Coalesce(Sum("amount"), Value(Decimal("0.0")), output_field=DecimalField()),
            total_net=Coalesce(Sum("net_payout_amount"), Value(Decimal("0.0")), output_field=DecimalField()),
        )

    def calculate_metrics(self, business, start_date, end_date, class_id=None, source="all"):
        if source == "membership":
            curr = self._get_membership_payment_aggregates(business, start_date, end_date)
            period_length_timedelta = end_date - start_date
            if period_length_timedelta < timedelta(days=1):
                period_length_timedelta = timedelta(days=1)
            previous_end_date = start_date - timedelta.resolution
            previous_start_date = previous_end_date - period_length_timedelta + timedelta.resolution
            prev = self._get_membership_payment_aggregates(business, previous_start_date, previous_end_date)
            current_total_gross_revenue = float(curr["total_amount"])
            current_total_net_payout = float(curr["total_net"])
            previous_total_gross_revenue = float(prev["total_amount"])
            platform_fees_actual = current_total_gross_revenue - current_total_net_payout
            revenue_growth = 0.0
            if previous_total_gross_revenue > 0:
                revenue_growth = ((current_total_gross_revenue - previous_total_gross_revenue) / previous_total_gross_revenue) * 100.0
            elif current_total_gross_revenue > 0:
                revenue_growth = 100.0
            return {
                "total_gross_revenue": round(current_total_gross_revenue, 2),
                "estimated_platform_fees": round(platform_fees_actual, 2),
                "estimated_net_revenue": round(current_total_net_payout, 2),
                "average_order_value": 0.0,
                "revenue_per_booker": 0.0,
                "revenue_growth": round(revenue_growth, 1),
                "revenue_per_spot": 0.0,
                "recurring_revenue": round(current_total_gross_revenue, 2),
            }

        current_period_qs = self.get_valid_bookings_queryset(
            business, start_date, end_date, class_id, source
        )
        period_length_timedelta = end_date - start_date
        if period_length_timedelta < timedelta(days=1):
            period_length_timedelta = timedelta(days=1)
        previous_end_date = start_date - timedelta.resolution
        previous_start_date = (
            previous_end_date - period_length_timedelta + timedelta.resolution
        )
        previous_period_qs = self.get_valid_bookings_queryset(
            business, previous_start_date, previous_end_date, class_id, source
        )

        current_aggregates = current_period_qs.aggregate(
            total_gross_revenue_decimal=Coalesce(
                Sum("amount_paid"), Value(Decimal("0.0")), output_field=DecimalField()
            ),
            total_net_payout_decimal=Coalesce(
                Sum("allocated_net_payout"),
                Value(Decimal("0.0")),
                output_field=DecimalField(),
            ),
            total_bookings=Count("id"),
            unique_bookers=Count("contact", distinct=True),
            total_participant_spots_decimal=Coalesce(
                Sum("participants"), Value(0), output_field=IntegerField()
            ),
        )
        current_total_gross_revenue = float(
            current_aggregates["total_gross_revenue_decimal"]
        )
        current_total_net_payout = float(
            current_aggregates["total_net_payout_decimal"]
        )
        current_total_bookings = current_aggregates["total_bookings"]
        current_unique_bookers = current_aggregates["unique_bookers"]
        current_total_participant_spots = int(
            current_aggregates["total_participant_spots_decimal"]
        )

        # Include membership payments when source is "all"
        if source == "all":
            mem_curr = self._get_membership_payment_aggregates(business, start_date, end_date)
            current_total_gross_revenue += float(mem_curr["total_amount"])
            current_total_net_payout += float(mem_curr["total_net"])

        previous_aggregates = previous_period_qs.aggregate(
            prev_gross_revenue_decimal=Coalesce(
                Sum("amount_paid"), Value(Decimal("0.0")), output_field=DecimalField()
            )
        )
        previous_total_gross_revenue = float(
            previous_aggregates["prev_gross_revenue_decimal"]
        )
        if source == "all":
            mem_prev = self._get_membership_payment_aggregates(
                business, previous_start_date, previous_end_date
            )
            previous_total_gross_revenue += float(mem_prev["total_amount"])

        average_order_value = (
            (current_total_gross_revenue / current_total_bookings)
            if current_total_bookings > 0
            else 0.0
        )
        revenue_per_booker = (
            (current_total_gross_revenue / current_unique_bookers)
            if current_unique_bookers > 0
            else 0.0
        )
        revenue_growth = 0.0
        if previous_total_gross_revenue > 0:
            revenue_growth = (
                (current_total_gross_revenue - previous_total_gross_revenue)
                / previous_total_gross_revenue
            ) * 100.0
        elif current_total_gross_revenue > 0 and previous_total_gross_revenue == 0:
            revenue_growth = 100.0

        platform_fees_actual = current_total_gross_revenue - current_total_net_payout
        estimated_platform_fees = round(platform_fees_actual, 2)
        estimated_net_revenue = round(current_total_net_payout, 2)

        revenue_per_spot = (
            (current_total_gross_revenue / current_total_participant_spots)
            if current_total_participant_spots > 0
            else 0.0
        )

        return {
            "total_gross_revenue": round(current_total_gross_revenue, 2),
            "estimated_platform_fees": round(estimated_platform_fees, 2),
            "estimated_net_revenue": round(estimated_net_revenue, 2),
            "average_order_value": round(average_order_value, 2),
            "revenue_per_booker": round(revenue_per_booker, 2),
            "revenue_growth": round(revenue_growth, 1),
            "revenue_per_spot": round(revenue_per_spot, 2),
            "recurring_revenue": 0.0,
        }

    def _get_membership_trends_by_date(self, business, start_date, end_date, business_pytz):
        """Daily membership payment totals in business timezone."""
        qs = MembershipPayment.objects.filter(
            membership__product__business=business,
            status="paid",
            created_at__range=[start_date, end_date],
        ).annotate(
            local_date=TruncDate(F("created_at"), tzinfo=business_pytz),
        ).values("local_date").annotate(
            gross_revenue_decimal=Coalesce(Sum("amount"), Value(Decimal("0.0")), output_field=DecimalField()),
            net_payout_decimal=Coalesce(Sum("net_payout_amount"), Value(Decimal("0.0")), output_field=DecimalField()),
        ).order_by("local_date")
        return {entry["local_date"].isoformat(): {"gross": float(entry["gross_revenue_decimal"]), "net": float(entry["net_payout_decimal"])} for entry in qs if entry["local_date"]}

    def get_revenue_trends(self, business, start_date, end_date, class_id=None, source="all"):
        business_pytz = pytz.timezone(business.business_timezone)
        all_dates_in_range_local = {}
        current_scan_local_date = start_date.astimezone(business_pytz).date()
        end_scan_local_date = end_date.astimezone(business_pytz).date()
        while current_scan_local_date <= end_scan_local_date:
            all_dates_in_range_local[current_scan_local_date.isoformat()] = {
                "gross_revenue": 0.0,
                "net_revenue": 0.0,
                "platform_fees": 0.0,
            }
            current_scan_local_date += timedelta(days=1)

        if source == "membership":
            membership_by_date = self._get_membership_trends_by_date(business, start_date, end_date, business_pytz)
            for date_iso, data in membership_by_date.items():
                if date_iso in all_dates_in_range_local:
                    all_dates_in_range_local[date_iso]["gross_revenue"] = data["gross"]
                    all_dates_in_range_local[date_iso]["net_revenue"] = data["net"]
                    all_dates_in_range_local[date_iso]["platform_fees"] = data["gross"] - data["net"]
            return [
                {"date": date_str, **rev_data}
                for date_str, rev_data in sorted(all_dates_in_range_local.items())
            ]

        valid_bookings_qs = self.get_valid_bookings_queryset(
            business, start_date, end_date, class_id, source
        )
        trends_qs = (
            valid_bookings_qs.annotate(
                local_booking_date_trunc=TruncDate(
                    F("booking_date"), tzinfo=business_pytz
                )
            )
            .values("local_booking_date_trunc")
            .annotate(
                gross_revenue_decimal=Coalesce(
                    Sum("amount_paid"),
                    Value(Decimal("0.0")),
                    output_field=DecimalField(),
                ),
                net_payout_decimal=Coalesce(
                    Sum("allocated_net_payout"),
                    Value(Decimal("0.0")),
                    output_field=DecimalField(),
                ),
            )
            .order_by("local_booking_date_trunc")
        )

        for entry in trends_qs:
            date_iso = entry["local_booking_date_trunc"].isoformat()
            gross_rev = entry["gross_revenue_decimal"]
            net_payout = entry["net_payout_decimal"]
            if date_iso in all_dates_in_range_local:
                gross_f = float(gross_rev)
                net_f = float(net_payout)
                all_dates_in_range_local[date_iso]["gross_revenue"] = gross_f
                all_dates_in_range_local[date_iso]["net_revenue"] = net_f
                all_dates_in_range_local[date_iso]["platform_fees"] = gross_f - net_f

        if source == "all":
            membership_by_date = self._get_membership_trends_by_date(business, start_date, end_date, business_pytz)
            for date_iso, data in membership_by_date.items():
                if date_iso in all_dates_in_range_local:
                    all_dates_in_range_local[date_iso]["gross_revenue"] += data["gross"]
                    all_dates_in_range_local[date_iso]["net_revenue"] += data["net"]
                    all_dates_in_range_local[date_iso]["platform_fees"] += data["gross"] - data["net"]

        formatted_trends = [
            {"date": date_str, **rev_data}
            for date_str, rev_data in sorted(all_dates_in_range_local.items())
        ]
        return formatted_trends

    def get_class_revenue(self, business, start_date, end_date, class_id_filter=None, source="all"):
        if source == "membership":
            return []
        valid_bookings_qs = self.get_valid_bookings_queryset(
            business, start_date, end_date, class_id_filter, source
        )

        # Booking-based totals (actual payout logic: matches webhook allocated_net_payout)
        class_booking_totals = (
            valid_bookings_qs.values(
                "schedule_instance__schedule__option__classId"
            )
            .annotate(
                gross_revenue_decimal=Coalesce(
                    Sum("amount_paid"),
                    Value(Decimal("0.0")),
                    output_field=DecimalField(),
                ),
                net_payout_decimal=Coalesce(
                    Sum("allocated_net_payout"),
                    Value(Decimal("0.0")),
                    output_field=DecimalField(),
                ),
            )
            .order_by("-gross_revenue_decimal")
        )

        # Widget/platform breakdown from Payment (for display only; one payment per booking)
        payment_by_class = (
            Payment.objects.filter(
                booking__in=valid_bookings_qs, status="succeeded"
            )
            .values("booking__schedule_instance__schedule__option__classId")
            .annotate(
                widget_revenue=Coalesce(
                    Sum(
                        "amount",
                        filter=Q(
                            metadata__original_stripe_metadata__booking_source__in=_WIDGET_SOURCE_LIST
                        ),
                    ),
                    Value(Decimal("0.0")),
                    output_field=DecimalField(),
                ),
                platform_revenue=Coalesce(
                    Sum(
                        "amount",
                        filter=~Q(
                            metadata__original_stripe_metadata__booking_source__in=_WIDGET_SOURCE_LIST
                        ),
                    ),
                    Value(Decimal("0.0")),
                    output_field=DecimalField(),
                ),
            )
        )
        payment_by_class_map = {
            item["booking__schedule_instance__schedule__option__classId"]: item
            for item in payment_by_class
            if item["booking__schedule_instance__schedule__option__classId"] is not None
        }

        class_ids = [
            item["schedule_instance__schedule__option__classId"]
            for item in class_booking_totals
            if item["schedule_instance__schedule__option__classId"] is not None
        ]
        class_titles_map = dict(
            ClassesMain.objects.filter(classId__in=class_ids).values_list(
                "classId", "title"
            )
        )

        result = []
        for item in class_booking_totals:
            class_id = item["schedule_instance__schedule__option__classId"]
            if class_id:
                gross_f = float(item["gross_revenue_decimal"])
                net_f = float(item["net_payout_decimal"])
                payment_info = payment_by_class_map.get(class_id, {})
                widget_rev = float(
                    payment_info.get("widget_revenue") or Decimal("0.0")
                )
                platform_rev = float(
                    payment_info.get("platform_revenue") or Decimal("0.0")
                )
                result.append(
                    {
                        "id": class_id,
                        "name": class_titles_map.get(class_id, f"Class ID {class_id}"),
                        "gross_revenue": gross_f,
                        "widget_revenue": widget_rev,
                        "platform_revenue": platform_rev,
                        "platform_fees": round(gross_f - net_f, 2),
                        "net_revenue": round(net_f, 2),
                    }
                )
        return result

    def get_revenue_by_booking_type(
        self, business, start_date, end_date, class_id=None, source="all"
    ):
        if source == "membership":
            agg = self._get_membership_payment_aggregates(business, start_date, end_date)
            total = float(agg["total_amount"])
            if total == 0:
                return []
            return [{"name": "Membership Payments", "value": total}]
        valid_bookings_qs = self.get_valid_bookings_queryset(
            business, start_date, end_date, class_id, source
        )

        revenue_by_type_data = (
            Payment.objects.filter(booking__in=valid_bookings_qs, status="succeeded")
            .annotate(
                booking_type_category=Case(
                    When(
                        metadata__original_stripe_metadata__booking_source__in=_WIDGET_SOURCE_LIST,
                        then=Value("Widget Booking"),
                    ),
                    default=Value("Platform Booking"),
                    output_field=CharField(),
                )
            )
            .values("booking_type_category")
            .annotate(value=Sum("amount"))
            .order_by("-value")
        )

        revenue_by_booking_type = [
            {"name": entry["booking_type_category"], "value": float(entry["value"])}
            for entry in revenue_by_type_data
        ]
        if source == "all":
            mem_agg = self._get_membership_payment_aggregates(business, start_date, end_date)
            mem_total = float(mem_agg["total_amount"])
            if mem_total > 0:
                revenue_by_booking_type.append({"name": "Membership Payments", "value": mem_total})
                revenue_by_booking_type.sort(key=lambda x: -x["value"])
        return revenue_by_booking_type

    def get(self, request):
        user = request.user
        business = self.get_business(user)
        if not business:
            raise PermissionDenied("You are not associated with a business.")
        if not user.has_perm("quickstart.view_business_revenue_analytics"):
            raise PermissionDenied(
                "You do not have permission to view revenue analytics."
            )

        try:
            start_date_utc, end_date_utc = self.get_date_range(request)
            class_id_filter = request.query_params.get("class_id")
            if class_id_filter and not class_id_filter.isdigit():
                raise ValidationError("Invalid class_id format.")
            class_id_filter = int(class_id_filter) if class_id_filter else None

            source_filter = request.query_params.get("source", "all")
            if source_filter not in ("widget", "marketplace", "all", "membership"):
                source_filter = "all"

            if source_filter == "widget" and not business_has_growth_or_advanced_widget_plan(
                business
            ):
                source_filter = "all"

            metrics = self.calculate_metrics(
                business, start_date_utc, end_date_utc, class_id_filter, source_filter
            )
            trends = self.get_revenue_trends(
                business, start_date_utc, end_date_utc, class_id_filter, source_filter
            )
            class_revenue_breakdown = self.get_class_revenue(
                business, start_date_utc, end_date_utc, class_id_filter, source_filter
            )
            revenue_by_booking_type = self.get_revenue_by_booking_type(
                business, start_date_utc, end_date_utc, class_id_filter, source_filter
            )

            if source_filter == "all" and not business_has_growth_or_advanced_widget_plan(
                business
            ):
                class_revenue_breakdown, revenue_by_booking_type = (
                    self._redact_widget_breakdown_for_basic_plan(
                        class_revenue_breakdown, revenue_by_booking_type
                    )
                )

            data = {
                "business_id": business.businessId,
                "business_name": business.businessName,
                "metrics": metrics,
                "revenue_trends": trends,
                "class_revenue": class_revenue_breakdown,
                "revenue_by_booking_type": revenue_by_booking_type,
            }
            return Response(data, status=status.HTTP_200_OK)
        except ValidationError as e:
            return Response({"error": e.detail}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as e:
            return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except Exception as e:
            logger.error(
                f"Error fetching revenue analytics for Business {business.businessId if business else 'N/A'}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {"error": "An unexpected error occurred."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def post(self, request):  # Export to CSV
        user = request.user
        business = self.get_business(user)
        if not business:
            raise PermissionDenied("Not associated with a business.")
        if not user.has_perm("quickstart.export_business_revenue_data"):
            raise PermissionDenied("No permission to export revenue data.")

        try:
            start_date_utc, end_date_utc = self.get_date_range(request)
            class_id_filter = request.query_params.get("class_id")
            if class_id_filter and not class_id_filter.isdigit():
                class_id_filter = None
            else:
                class_id_filter = int(class_id_filter) if class_id_filter else None

            response = HttpResponse(content_type="text/csv")
            filename = f"{business.businessName.replace(' ', '_')}_revenue_report_{start_date_utc.strftime('%Y%m%d')}_{end_date_utc.strftime('%Y%m%d')}.csv"
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            writer = csv.writer(response)

            writer.writerow(["Revenue Report"])
            writer.writerow(["Business:", business.businessName])
            writer.writerow(
                [
                    "Period:",
                    f"{start_date_utc.date().strftime('%Y-%m-%d')} to {end_date_utc.date().strftime('%Y-%m-%d')}",
                ]
            )
            writer.writerow([])
            writer.writerow(
                [
                    "Note:",
                    "All amounts reflect actual amounts after any business discounts, global discounts, and gift cards applied at checkout. Net Payout is the amount allocated to the business.",
                ]
            )

            # --- 1. Metrics Summary (uses actual payout and platform take from bookings) ---
            metrics = self.calculate_metrics(
                business, start_date_utc, end_date_utc, class_id_filter
            )
            HST_RATE = Decimal("0.13")
            total_amount_collected = Decimal(str(metrics["total_gross_revenue"]))
            platform_fees_actual = Decimal(str(metrics["estimated_platform_fees"]))
            net_revenue_actual = Decimal(str(metrics["estimated_net_revenue"]))

            writer.writerow(["Key Metrics Summary", "Value"])
            writer.writerow(
                [
                    "Total Amount Collected from Customers (incl. tax)",
                    f"${total_amount_collected:.2f}",
                ]
            )
            writer.writerow(
                ["Platform Fees (actual take)", f"${platform_fees_actual:.2f}"]
            )
            writer.writerow(
                ["Net Payout to Business (actual)", f"${net_revenue_actual:.2f}"]
            )
            writer.writerow([])
            
            # --- 2. Daily Trends ---
            trends = self.get_revenue_trends(
                business, start_date_utc, end_date_utc, class_id_filter
            )
            writer.writerow(["Daily Revenue Detail (Local Business Time, incl. Tax)"])
            writer.writerow(["Date", "Gross Revenue", "Platform Fees", "Net Revenue"])
            for entry in trends:
                writer.writerow(
                    [
                        entry["date"],
                        f"${entry['gross_revenue']:.2f}",
                        f"${entry['platform_fees']:.2f}",
                        f"${entry['net_revenue']:.2f}",
                    ]
                )
            writer.writerow([])

            # --- 3. Class Revenue ---
            class_revenue = self.get_class_revenue(
                business, start_date_utc, end_date_utc, class_id_filter
            )
            writer.writerow(["Revenue by Class (incl. Tax)"])
            writer.writerow(
                ["Class Name", "Gross Revenue", "Platform Fees", "Net Revenue"]
            )
            for entry in class_revenue:
                writer.writerow(
                    [
                        entry["name"],
                        f"${entry['gross_revenue']:.2f}",
                        f"${entry['platform_fees']:.2f}",
                        f"${entry['net_revenue']:.2f}",
                    ]
                )
            writer.writerow([])

            # --- 4. Detailed Transaction Report ---
            writer.writerow(["Detailed Transaction Report for Accounting"])
            writer.writerow(
                [
                    "Booking Ref",
                    "Booking Date (UTC)",
                    "Class Date (Local)",
                    "Class Name",
                    "Booker Name",
                    "Booker Email",
                    "Participants",
                    "Subtotal (Pre-Tax)",
                    "Tax Collected from Student",
                    "Total Amount Paid",
                    "Platform Fee (Pre-tax)",
                    "Tax on Platform Fee (ITC for Business)",
                    "Net Payout to Business",
                    "Business Discount (codes)",
                    "Business Discount ($)",
                    "Global Discount (names)",
                    "Global Discount ($)",
                    "Gift Card Applied ($)",
                    "Payment Status",
                    "Booking Status",
                ]
            )

            detailed_bookings_qs = (
                self.get_valid_bookings_queryset(
                    business, start_date_utc, end_date_utc, class_id_filter
                )
                .select_related(
                    "user", "contact", "schedule_instance__schedule__option__classId"
                )
                .prefetch_related(
                    "payments",
                    "applied_global_discounts__global_discount",
                    "discounts",
                    "applieddiscount_set",
                )
                .order_by("booking_date")
            )

            for booking in detailed_bookings_qs:
                # Retrieve booker details
                booker_name, booker_email = ("N/A", "N/A")
                if booking.user:
                    booker_name = (
                        f"{booking.user.first_name} {booking.user.last_name}".strip()
                    )
                    booker_email = booking.user.email
                elif booking.contact:
                    booker_name = f"{booking.contact.first_name} {booking.contact.last_name}".strip()
                    booker_email = booking.contact.email

                total_paid = booking.amount_paid
                net_payout = booking.allocated_net_payout

                pay = None
                for p in booking.payments.all():
                    if p.status == "succeeded":
                        pay = p
                        break

                if pay and pay.amount and pay.amount > 0:
                    share = (total_paid / pay.amount).quantize(Decimal("0.0001"))
                    tax_collected = (pay.tax_amount * share).quantize(Decimal("0.01"))
                    platform_fee_pre_tax = (pay.platform_fee_amount * share).quantize(
                        Decimal("0.01")
                    )
                    hst_on_fee = (pay.platform_fee_tax * share).quantize(Decimal("0.01"))
                else:
                    subtotal_est = total_paid / (Decimal("1.0") + HST_RATE)
                    tax_collected = (total_paid - subtotal_est).quantize(Decimal("0.01"))
                    fee_rate = self._platform_fee_rate_for_booking(booking, business)
                    platform_fee_pre_tax = (subtotal_est * fee_rate).quantize(
                        Decimal("0.01")
                    )
                    hst_on_fee = (platform_fee_pre_tax * HST_RATE).quantize(Decimal("0.01"))

                subtotal = (total_paid - tax_collected).quantize(Decimal("0.01"))

                if net_payout == Decimal("0.00") and total_paid > 0:
                    net_payout = (
                        subtotal - platform_fee_pre_tax + (tax_collected - hst_on_fee)
                    ).quantize(Decimal("0.01"))

                business_discount_names = "—"
                business_discount_amt = Decimal("0.00")
                if booking.discounts.exists():
                    names = [d.name or (d.code or "—") for d in booking.discounts.all()]
                    business_discount_names = ", ".join(names) if names else "—"
                for ad in booking.applieddiscount_set.all():
                    business_discount_amt += ad.amount_saved

                global_discount_names = "—"
                global_discount_amt = Decimal("0.00")
                if booking.applied_global_discounts.exists():
                    names = [
                        a.global_discount.name
                        for a in booking.applied_global_discounts.all()
                    ]
                    global_discount_names = ", ".join(names) if names else "—"
                    for a in booking.applied_global_discounts.all():
                        global_discount_amt += a.amount_saved

                gift_card_amt = self._gift_card_amount_from_payment(pay, total_paid)

                writer.writerow(
                    [
                        booking.user_facing_reference or f"ID-{booking.id}",
                        booking.booking_date.strftime("%Y-%m-%d %H:%M"),
                        booking.schedule_instance.date.strftime("%Y-%m-%d"),
                        booking.schedule_instance.schedule.option.classId.title,
                        booker_name,
                        booker_email,
                        booking.participants,
                        f"${subtotal:.2f}",
                        f"${tax_collected:.2f}",
                        f"${total_paid:.2f}",
                        f"${platform_fee_pre_tax:.2f}",
                        f"${hst_on_fee:.2f}",
                        f"${net_payout:.2f}",
                        business_discount_names,
                        f"${business_discount_amt:.2f}",
                        global_discount_names,
                        f"${global_discount_amt:.2f}",
                        f"${gift_card_amt:.2f}",
                        booking.get_payment_status_display(),
                        booking.get_status_display(),
                    ]
                )

            writer.writerow([])
            writer.writerow(["Membership payment detail"])
            writer.writerow(
                [
                    "Payment ID",
                    "Paid At (UTC)",
                    "Amount (gross)",
                    "Platform Fee (pre-tax)",
                    "Net Payout to Business",
                    "Status",
                ]
            )
            membership_rows = (
                MembershipPayment.objects.filter(
                    membership__product__business=business,
                    status="paid",
                    created_at__range=[start_date_utc, end_date_utc],
                )
                .select_related("membership", "membership__product")
                .order_by("created_at")
            )
            for mp in membership_rows:
                writer.writerow(
                    [
                        str(mp.id),
                        mp.created_at.strftime("%Y-%m-%d %H:%M"),
                        f"${mp.amount:.2f}",
                        f"${mp.platform_fee_amount:.2f}",
                        f"${mp.net_payout_amount:.2f}",
                        mp.get_status_display(),
                    ]
                )

            logger.info(
                f"Revenue report exported for Business '{business.businessName}' by {user.email}"
            )
            return response
        except ValidationError as e:
            return Response({"error": e.detail}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionDenied as e:
            return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except Exception as e:
            logger.error(
                f"Error exporting revenue report for Business {business.businessId if business else 'N/A'}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {"error": "Error exporting report."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )