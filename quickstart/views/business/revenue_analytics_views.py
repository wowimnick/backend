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
    PartnerTier,
    Payment,
)

logger = logging.getLogger(__name__)


class RevenueAnalyticsView(views.APIView):
    permission_classes = [IsAuthenticated]

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
            self, business, start_date, end_date, class_id=None
        ):
            """
            Returns all bookings that contribute to revenue (paid).
            UPDATED: Now includes ALL bookings for a course, because revenue is split 1/N
            across them. We no longer filter for just the first booking.
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

    def calculate_metrics(self, business, start_date, end_date, class_id=None):
        current_period_qs = self.get_valid_bookings_queryset(
            business, start_date, end_date, class_id
        )
        period_length_timedelta = end_date - start_date
        if period_length_timedelta < timedelta(days=1):
            period_length_timedelta = timedelta(days=1)
        previous_end_date = start_date - timedelta.resolution
        previous_start_date = (
            previous_end_date - period_length_timedelta + timedelta.resolution
        )
        previous_period_qs = self.get_valid_bookings_queryset(
            business, previous_start_date, previous_end_date, class_id
        )

        current_aggregates = current_period_qs.aggregate(
            total_gross_revenue_decimal=Coalesce(
                Sum("amount_paid"), Value(Decimal("0.0")), output_field=DecimalField()
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
        current_total_bookings = current_aggregates["total_bookings"]
        current_unique_bookers = current_aggregates["unique_bookers"]
        current_total_participant_spots = int(
            current_aggregates["total_participant_spots_decimal"]
        )

        previous_aggregates = previous_period_qs.aggregate(
            prev_gross_revenue_decimal=Coalesce(
                Sum("amount_paid"), Value(Decimal("0.0")), output_field=DecimalField()
            )
        )
        previous_total_gross_revenue = float(
            previous_aggregates["prev_gross_revenue_decimal"]
        )

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

        platform_fee_rate = self._get_fee_rate_for_business(business)

        # --- FIX START: Correct fee calculation based on pre-tax amount ---
        # Assuming a constant 13% HST rate for this calculation, adjust if variable.
        HST_RATE = Decimal("0.13")
        total_amount_collected_decimal = current_aggregates[
            "total_gross_revenue_decimal"
        ]

        # This logic assumes 'amount_paid' on Booking is the pre-tax subtotal.
        gross_sales_pre_tax = total_amount_collected_decimal

        estimated_platform_fees = float(gross_sales_pre_tax * platform_fee_rate)
        estimated_net_revenue = float(
            gross_sales_pre_tax * (Decimal("1.0") - platform_fee_rate)
        )
        # --- FIX END ---

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
            "recurring_revenue": 0.0,  # Placeholder
        }

    def get_revenue_trends(self, business, start_date, end_date, class_id=None):
        valid_bookings_qs = self.get_valid_bookings_queryset(
            business, start_date, end_date, class_id
        )
        business_pytz = pytz.timezone(business.business_timezone)

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
                )
            )
            .order_by("local_booking_date_trunc")
        )

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

        platform_fee_rate = self._get_fee_rate_for_business(business)

        for entry in trends_qs:
            date_iso = entry["local_booking_date_trunc"].isoformat()
            gross_rev = entry["gross_revenue_decimal"]
            if date_iso in all_dates_in_range_local:
                all_dates_in_range_local[date_iso]["gross_revenue"] = float(gross_rev)
                all_dates_in_range_local[date_iso]["platform_fees"] = float(
                    gross_rev * platform_fee_rate
                )
                all_dates_in_range_local[date_iso]["net_revenue"] = float(
                    gross_rev * (Decimal("1.0") - platform_fee_rate)
                )

        formatted_trends = [
            {"date": date_str, **rev_data}
            for date_str, rev_data in sorted(all_dates_in_range_local.items())
        ]
        return formatted_trends

    def get_class_revenue(self, business, start_date, end_date, class_id_filter=None):
        valid_bookings_qs = self.get_valid_bookings_queryset(
            business, start_date, end_date, class_id_filter
        )

        # Query payments related to the valid bookings to get the source metadata
        class_revenue_data = (
            Payment.objects.filter(booking__in=valid_bookings_qs, status="succeeded")
            .values("booking__schedule_instance__schedule__option__classId")
            .annotate(
                # Conditionally sum revenue for widget bookings
                widget_revenue=Coalesce(
                    Sum(
                        "amount",
                        filter=Q(
                            metadata__original_stripe_metadata__booking_source="widget"
                        ),
                    ),
                    Value(Decimal("0.0")),
                    output_field=DecimalField(),
                ),
                # Conditionally sum revenue for platform bookings
                platform_revenue=Coalesce(
                    Sum(
                        "amount",
                        filter=Q(
                            metadata__original_stripe_metadata__booking_source__isnull=True
                        )
                        | Q(
                            metadata__original_stripe_metadata__booking_source__ne="widget"
                        ),
                    ),
                    Value(Decimal("0.0")),
                    output_field=DecimalField(),
                ),
                total_gross_revenue=Sum("amount"),
            )
            .order_by("-total_gross_revenue")
        )

        class_ids = [
            item["booking__schedule_instance__schedule__option__classId"]
            for item in class_revenue_data
            if item["booking__schedule_instance__schedule__option__classId"] is not None
        ]

        class_titles_map = dict(
            ClassesMain.objects.filter(classId__in=class_ids).values_list(
                "classId", "title"
            )
        )

        # --- FIX: Get fee rate and calculate breakdown ---
        platform_fee_rate = self._get_fee_rate_for_business(business)
        HST_RATE = Decimal("0.13")

        result = []
        for item in class_revenue_data:
            class_id = item["booking__schedule_instance__schedule__option__classId"]
            if class_id:
                gross_rev = item["total_gross_revenue"]
                widget_rev = item["widget_revenue"]
                platform_rev = item["platform_revenue"]
                
                # Calculate Net/Fees
                gross_pre_tax = (gross_rev / (Decimal("1.0") + HST_RATE))
                fees = gross_pre_tax * platform_fee_rate
                net = gross_pre_tax - fees

                result.append(
                    {
                        "id": class_id,
                        "name": class_titles_map.get(class_id, f"Class ID {class_id}"),
                        "gross_revenue": float(gross_rev),
                        "widget_revenue": float(widget_rev),
                        "platform_revenue": float(platform_rev),
                        # ADDED THESE KEYS TO FIX THE EXPORT ERROR
                        "platform_fees": float(fees),
                        "net_revenue": float(net),
                    }
                )
        return result

    def get_revenue_by_booking_type(
        self, business, start_date, end_date, class_id=None
    ):
        valid_bookings_qs = self.get_valid_bookings_queryset(
            business, start_date, end_date, class_id
        )

        revenue_by_type_data = (
            Payment.objects.filter(booking__in=valid_bookings_qs, status="succeeded")
            .annotate(
                booking_type_category=Case(
                    When(
                        metadata__original_stripe_metadata__booking_source="widget",
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

            metrics = self.calculate_metrics(
                business, start_date_utc, end_date_utc, class_id_filter
            )
            trends = self.get_revenue_trends(
                business, start_date_utc, end_date_utc, class_id_filter
            )
            class_revenue_breakdown = self.get_class_revenue(
                business, start_date_utc, end_date_utc, class_id_filter
            )
            revenue_by_booking_type = self.get_revenue_by_booking_type(
                business, start_date_utc, end_date_utc, class_id_filter
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
            
            # --- 1. Metrics Summary ---
            metrics = self.calculate_metrics(
                business, start_date_utc, end_date_utc, class_id_filter
            )
            platform_fee_rate = self._get_fee_rate_for_business(business)
            fee_percentage = platform_fee_rate * 100
            fee_percentage_text = f"{fee_percentage:.0f}%"

            HST_RATE = Decimal("0.13")
            total_amount_collected = Decimal(str(metrics["total_gross_revenue"]))

            gross_sales_pre_tax = (total_amount_collected / (1 + HST_RATE)).quantize(
                Decimal("0.01")
            )
            tax_collected_from_customers = (
                total_amount_collected - gross_sales_pre_tax
            ).quantize(Decimal("0.01"))

            platform_fees_pre_tax = (gross_sales_pre_tax * platform_fee_rate).quantize(
                Decimal("0.01")
            )
            net_revenue_pre_tax = (
                gross_sales_pre_tax - platform_fees_pre_tax
            ).quantize(Decimal("0.01"))

            writer.writerow(["Key Metrics Summary", "Value"])
            writer.writerow(["Gross Sales (Pre-Tax)", f"${gross_sales_pre_tax:.2f}"])
            writer.writerow(
                [
                    "Tax Collected from Customers (HST)",
                    f"${tax_collected_from_customers:.2f}",
                ]
            )
            writer.writerow(
                [
                    "Total Amount Collected from Customers",
                    f"${total_amount_collected:.2f}",
                ]
            )
            writer.writerow(
                [
                    f"Estimated Platform Fees ({fee_percentage_text}, Pre-Tax)",
                    f"${platform_fees_pre_tax:.2f}",
                ]
            )
            writer.writerow(
                ["Estimated Net Revenue (Pre-Tax)", f"${net_revenue_pre_tax:.2f}"]
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

            # --- 4. Detailed Transaction Report (FIXED LOGIC) ---
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
                    "Tax Collected from Student (HST)",
                    "Total Amount Paid",
                    f"Platform Fee ({fee_percentage_text}, Pre-tax)",
                    "HST on Platform Fee (ITC for Business)",
                    "Net Payout to Business",
                    "Payment Status",
                    "Booking Status"
                ]
            )

            detailed_bookings_qs = (
                self.get_valid_bookings_queryset(
                    business, start_date_utc, end_date_utc, class_id_filter
                )
                .select_related(
                    "user", "contact", "schedule_instance__schedule__option__classId"
                )
                .prefetch_related("payments")
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

                # --- CRITICAL FIX: Use Booking values, NOT Payment values ---
                # This ensures course sessions show $113 each, not $565 each.
                total_paid = booking.amount_paid
                net_payout = booking.allocated_net_payout
                
                # Calculate breakdowns based on the SPLIT booking amount
                subtotal = total_paid / (Decimal("1.0") + HST_RATE)
                tax_collected = total_paid - subtotal
                
                # Calculate fees based on the SPLIT amount
                platform_fee_pre_tax = subtotal * platform_fee_rate
                hst_on_fee = platform_fee_pre_tax * HST_RATE

                # Fallback for older data where allocated_net_payout might be 0
                if net_payout == Decimal("0.00") and total_paid > 0:
                     net_payout = total_paid - (platform_fee_pre_tax + hst_on_fee + tax_collected)

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
                        booking.get_payment_status_display(),
                        booking.get_status_display(),
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