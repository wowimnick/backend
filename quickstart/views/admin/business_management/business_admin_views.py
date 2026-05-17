from collections import defaultdict
from decimal import Decimal
from django.db.models import (
    Count,
    Sum,
    Avg,
    Case,
    When,
    F,
    DecimalField,
    Q,
    Value,
    CharField,
    Exists,
    OuterRef,
    Subquery,
    BooleanField,
)
from django.db.models.functions import (
    Coalesce,
    TruncDay,
    TruncMonth,
    TruncWeek,
    TruncQuarter,
    TruncYear,
)
from rest_framework.pagination import PageNumberPagination
from django.db.models import FloatField, IntegerField, BooleanField, DateTimeField
from django.contrib.gis.db.models.functions import Centroid
from django.utils import timezone
from datetime import datetime, timedelta
from rest_framework import viewsets, status, filters, generics
from rest_framework.decorators import action
from rest_framework.response import Response
import csv
from django.http import Http404, HttpResponse
import logging

from quickstart.utils.permissions import (
    IsAuthenticated,
    BasePermission,
    CanAccessBusinessAdmin,
    CanManageTargetBusiness,
    CanAccessClassAdmin,
    user_can_manage,
)
from quickstart.models import (
    AuditLog,
    BusinessInfo,
    BusinessStaff,
    ClassCollection,
    ClassOption,
    Booking,
    ClassesMain,
    Payment,
    Reviews,
    GeographicBoundary,
    Role,
    SearchLog,
)
from quickstart.serializers.admin.business_management.admin_business_serializers import (
    AdminBusinessDetailSerializer,
    AdminBusinessListSerializer,
    GeographicBoundaryDataSerializer,
)
from quickstart.views.admin.metrics_time_windows import get_admin_metrics_window
from quickstart.constants.search_location_presets import EXPLORE_LOCATION_PRESET_LABELS

logger = logging.getLogger(__name__)

# Rolling window for "engaged active" KPI (login within window AND at least one booking ever)
ENGAGED_LOGIN_LOOKBACK_DAYS = 30


def count_engaged_businesses(login_start_dt, login_end_exclusive_dt):
    """
    Businesses where an owner or accepted staff logged in during [login_start_dt, login_end_exclusive_dt)
    AND the business has at least one Booking (any status), via class schedule chain.
    """
    login_user_ids = list(
        AuditLog.objects.filter(
            action="login",
            timestamp__gte=login_start_dt,
            timestamp__lt=login_end_exclusive_dt,
            user_id__isnull=False,
        )
        .values_list("user_id", flat=True)
        .distinct()
    )
    if not login_user_ids:
        return 0
    owner_biz_ids = set(
        BusinessInfo.objects.filter(owner_id__in=login_user_ids).values_list(
            "pk", flat=True
        )
    )
    staff_biz_ids = set(
        BusinessStaff.objects.filter(
            user_id__in=login_user_ids,
            status=BusinessStaff.StaffStatus.ACCEPTED,
        ).values_list("business_id", flat=True)
    )
    candidates = owner_biz_ids | staff_biz_ids
    if not candidates:
        return 0
    has_booking = Exists(
        Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=OuterRef("pk"),
        )
    )
    return (
        BusinessInfo.objects.filter(pk__in=candidates)
        .filter(has_booking)
        .distinct()
        .count()
    )


def _normalize_search_location_key(value):
    if not value:
        return ""
    return " ".join(str(value).strip().lower().split())


class AdminBusinessPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


# --- ViewSet ---


class BusinessAdminViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing Businesses (Uses Django Permissions & Hierarchy)
    """

    permission_classes = [IsAuthenticated, CanAccessBusinessAdmin]
    pagination_class = AdminBusinessPagination

    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == "list":
            return AdminBusinessListSerializer
        return AdminBusinessDetailSerializer

    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "businessName",
        "businessCity",
        "businessState",
        "businessType",
        "owner__email",
        "owner__first_name",
        "owner__last_name",
    ]
    ordering_fields = [
        "businessName",
        "createdAt",
        "featured",
        "rating",
        "classes_count",
        "bookings_count",
        "revenue",
        "status",
    ]
    ordering = ["-createdAt"]

    def get_queryset(self):
        """Return queryset with annotations for admin views using Subqueries"""
        # FIX: Removed 'classCategory' from select_related as it no longer exists on BusinessInfo.
        queryset = BusinessInfo.objects.select_related("owner", "owner__role").all()

        classes_subquery = Subquery(
            ClassesMain.objects.filter(businessId=OuterRef("pk"))
            .values("businessId")
            .annotate(c=Count("pk"))
            .values("c"),
            output_field=IntegerField(),
        )
        bookings_subquery = Subquery(
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=OuterRef("pk"),
                status__in=["confirmed", "completed"],
            )
            .values("schedule_instance__schedule__option__classId__businessId")
            .annotate(c=Count("pk", distinct=True))
            .values("c"),
            output_field=IntegerField(),
        )
        revenue_subquery = Subquery(
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=OuterRef("pk"),
                status__in=["confirmed", "completed"],
            )
            .values("schedule_instance__schedule__option__classId__businessId")
            .annotate(s=Sum("amount_paid"))
            .values("s"),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        review_count_subquery = Subquery(
            Reviews.objects.filter(businessId=OuterRef("pk"), status="approved")
            .values("businessId")
            .annotate(c=Count("pk"))
            .values("c"),
            output_field=IntegerField(),
        )
        rating_subquery = Subquery(
            Reviews.objects.filter(businessId=OuterRef("pk"), status="approved")
            .values("businessId")
            .annotate(avg=Avg("rating"))
            .values("avg"),
            output_field=FloatField(),
        )
        has_active_schedules_subquery = Exists(
            ClassOption.objects.filter(
                classId__businessId=OuterRef("pk"),
                schedules__instances__status="scheduled",
                schedules__instances__date__gte=timezone.now().date(),
            )
        )

        engaged_login_cutoff = timezone.now() - timedelta(days=ENGAGED_LOGIN_LOOKBACK_DAYS)
        owner_login_engaged_sq = Exists(
            AuditLog.objects.filter(
                action="login",
                timestamp__gte=engaged_login_cutoff,
                user_id=OuterRef("owner_id"),
            )
        )
        # Correlate to BusinessInfo, not AuditLog: nesting Subquery under AuditLog
        # made OuterRef("pk") resolve to AuditLog.id (UUID), causing integer = uuid
        # against business_staff.business_id.
        staff_login_engaged_sq = Exists(
            BusinessStaff.objects.filter(
                business=OuterRef("pk"),
                status=BusinessStaff.StaffStatus.ACCEPTED,
            )
            .exclude(user_id__isnull=True)
            .filter(
                Exists(
                    AuditLog.objects.filter(
                        action="login",
                        timestamp__gte=engaged_login_cutoff,
                        user_id=OuterRef("user_id"),
                    )
                )
            )
        )
        has_any_booking_sq = Exists(
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=OuterRef(
                    "pk"
                ),
            )
        )

        queryset = queryset.annotate(
            has_active_schedules=has_active_schedules_subquery,
            classes_count=Coalesce(classes_subquery, 0),
            bookings_count=Coalesce(bookings_subquery, 0),
            revenue=Coalesce(revenue_subquery, Value(Decimal("0.00"))),
            rating=Coalesce(rating_subquery, Value(0.0)),
            review_count=Coalesce(review_count_subquery, 0),
            status=Case(
                When(
                    Q(isActive=True) & Q(has_active_schedules=True),
                    then=Value("active"),
                ),
                When(
                    Q(isActive=True) & Q(has_active_schedules=False),
                    then=Value("no_schedules"),
                ),
                When(verificationStatus="pending", then=Value("pending")),
                default=Value("inactive"),
                output_field=CharField(max_length=20),
            ),
            _owner_login_engaged=owner_login_engaged_sq,
            _staff_login_engaged=staff_login_engaged_sq,
            _has_any_booking_engaged=has_any_booking_sq,
        ).annotate(
            is_engaged=Case(
                When(
                    (
                        Q(_owner_login_engaged=True)
                        | Q(_staff_login_engaged=True)
                    )
                    & Q(_has_any_booking_engaged=True),
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )

        status_param = self.request.query_params.get("status", None)
        featured = self.request.query_params.get("featured", None)

        if status_param:
            queryset = queryset.filter(status=status_param)
        if featured is not None:
            is_featured = str(featured).lower() in ["true", "1", "yes"]
            queryset = queryset.filter(featured=is_featured)

        province_param = (self.request.query_params.get("province") or "").strip()
        if province_param:
            queryset = queryset.filter(businessState__iexact=province_param)

        verification_status = (
            self.request.query_params.get("verification_status") or ""
        ).strip()
        if verification_status:
            queryset = queryset.filter(verificationStatus=verification_status)

        revenue_tier = (self.request.query_params.get("revenue_tier") or "").strip()
        if revenue_tier == "under_1k":
            queryset = queryset.filter(revenue__lt=1000)
        elif revenue_tier == "1k_10k":
            queryset = queryset.filter(revenue__gte=1000, revenue__lt=10000)
        elif revenue_tier == "10k_plus":
            queryset = queryset.filter(revenue__gte=10000)

        engaged_query = (
            self.request.query_params.get("engaged") or ""
        ).strip().lower()
        if engaged_query in ("true", "1", "yes"):
            queryset = queryset.filter(is_engaged=True)
        elif engaged_query in ("false", "0", "no"):
            queryset = queryset.filter(is_engaged=False)

        if self.action in ("retrieve", "update", "partial_update", "metrics"):
            queryset = queryset.prefetch_related("managers")

        return queryset

    def list(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_businessinfo"):
            self.permission_denied(
                request, message="You do not have permission to view businesses."
            )
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_businessinfo"):
            self.permission_denied(
                request, message="You do not have permission to view business details."
            )
        queryset = self.get_queryset()
        try:
            instance = queryset.get(pk=kwargs["pk"])
        except BusinessInfo.DoesNotExist:
            raise Http404("Business not found.")
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", True)
        
        # 1. Permission Checks
        if not request.user.has_perm("quickstart.change_businessinfo"):
            self.permission_denied(
                request, message="You do not have permission to update businesses."
            )
            
        instance = self.get_object()
        
        # 2. Hierarchy Check (Ensure admin can manage this specific owner)
        if not user_can_manage(request.user, instance.owner):
            self.permission_denied(
                request,
                message="You cannot manage this business due to hierarchy restrictions.",
            )

        # 3. Capture Old State (Specifically isActive)
        old_is_active = instance.isActive
        
        # 4. Perform the Update
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        
        logger.info(
            f"Business '{instance.businessName}' (ID: {instance.pk}) updated by Admin {request.user.email}"
        )
        
        new_is_active = serializer.instance.isActive
        
        # 5. Check for Status Change & Trigger Revalidation
        if old_is_active != new_is_active:
            action_code = (
                "business_activate" if new_is_active else "business_deactivate"
            )
            details = f"Business '{instance.businessName}' was {'activated' if new_is_active else 'deactivated'} by admin."
            self._log_business_action(instance, action_code, details, request)

            try:
                from quickstart.utils.revalidation import trigger_nextjs_revalidation, trigger_multiple_revalidations

                # 1. Revalidate the Business Page itself
                if instance.slug:
                    trigger_nextjs_revalidation(tag=f"business-{instance.slug}")

                # 2. Collect tags for all classes belonging to this business
                #    (Since the business status affects the visibility of ALL its classes)
                business_classes = ClassesMain.objects.filter(businessId=instance)

                tags_to_revalidate = [
                    "classes-search",
                    "homepage-classes",
                    "businesses-list",
                    "businesses",
                    "public-businesses",
                ]

                for class_obj in business_classes:
                    # Tag for the class detail page
                    if class_obj.slug:
                        tags_to_revalidate.append(f"class-{class_obj.slug}")

                # 3. Trigger Bulk Revalidation
                if tags_to_revalidate:
                    unique_tags = list(set(tags_to_revalidate))
                    trigger_multiple_revalidations(tags=unique_tags)
                    logger.info(
                        f"Triggered revalidation for {len(unique_tags)} tags due to business status change (ID: {instance.pk})."
                    )

            except ImportError:
                logger.warning("Revalidation skipped: 'quickstart.utils.nextjs_utils' not found.")
            except Exception as e:
                logger.error(f"Error triggering revalidation during business update: {e}", exc_info=True)

        else:
            self._log_business_action(
                instance,
                "business_update",
                f"Business '{instance.businessName}' details updated by admin.",
                request,
            )
            
        if getattr(instance, "_prefetched_objects_cache", None):
            instance._prefetched_objects_cache = {}
            
        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.delete_businessinfo"):
            self.permission_denied(
                request, message="You do not have permission to delete businesses."
            )
        instance = self.get_object()
        business_name = instance.businessName
        owner = instance.owner  # Get a reference to the owner before deletion

        if not user_can_manage(request.user, owner):
            self.permission_denied(
                request,
                message="You cannot delete this business due to hierarchy restrictions.",
            )

        if Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=instance
        ).exists():
            details = f"Attempted to delete business '{business_name}' which has existing bookings. Action denied."
            logger.warning(details)
            self._log_business_action(
                instance, "business_delete_failed", details, request
            )
            return Response(
                {
                    "error": "Cannot delete business with existing bookings. Please deactivate it instead."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        has_other_businesses = (
            BusinessInfo.objects.filter(owner=owner).exclude(pk=instance.pk).exists()
        )

        if not has_other_businesses:
            try:
                student_role = Role.objects.get(name="Student")
                if owner.role != student_role:
                    original_role_name = owner.role.name if owner.role else "None"
                    owner.role = student_role
                    owner.save(update_fields=["role"])

                    # Log this specific action for a clear audit trail
                    log_details = f"User {owner.email}'s role was demoted from '{original_role_name}' to 'Student' because their last business '{business_name}' was deleted."
                    logger.info(log_details)
                    AuditLog.objects.create(
                        user=request.user,
                        user_email=request.user.email,
                        action="role_change",
                        details=log_details,
                        target_user=owner,
                        target_model="CustomUser",
                        target_id=str(owner.userId),
                        ip_address=request.META.get("REMOTE_ADDR"),
                        user_agent=request.META.get("HTTP_USER_AGENT", ""),
                        metadata={"reason": "Last business deleted"},
                    )

            except Role.DoesNotExist:
                # Log a critical error if the 'Student' role is missing
                logger.error(
                    f"CRITICAL: The 'Student' role does not exist. Cannot demote user {owner.email} after business deletion."
                )

        logger.warning(
            f"Business '{business_name}' (ID: {instance.pk}) will be deleted by Admin {request.user.email}"
        )
        self._log_business_action(
            instance,
            "business_delete",
            f"Business '{business_name}' deleted by admin.",
            request,
        )
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessBusinessAdmin],
    )
    def toggle_feature(self, request, pk=None):
        """
        UPDATED: Add revalidation for all business classes when featured status changes
        """
        if not request.user.has_perm("quickstart.toggle_business_feature"):
            self.permission_denied(
                request,
                message="You do not have permission to feature/unfeature businesses.",
            )
        business = self.get_object()
        if not user_can_manage(request.user, business.owner):
            self.permission_denied(
                request,
                message="You cannot manage this business due to hierarchy restrictions.",
            )
        featured = request.data.get("featured", not business.featured)
        if not isinstance(featured, bool):
            try:
                featured = str(featured).lower() in ["true", "1", "yes"]
            except Exception:
                return Response(
                    {
                        "detail": "Invalid value for featured status (must be true or false)."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        business.featured = featured
        business.save(update_fields=["featured"])
        action_text = "featured" if featured else "unfeatured"
        action_code = "business_feature" if featured else "business_unfeature"
        details = f"Business '{business.businessName}' was {action_text} by admin."
        logger.info(f"{details} (Admin: {request.user.email})")
        self._log_business_action(business, action_code, details, request)

        # --- ADDED: Trigger revalidation for business and all its classes ---
        # 1. Revalidate the business page
        if hasattr(business, "slug") and business.slug:
            trigger_nextjs_revalidation(tag=f"business-{business.slug}")
            logger.info(f"Revalidated business page: business-{business.slug}")

        # 2. Revalidate all classes belonging to this business
        business_classes = ClassesMain.objects.filter(businessId=business)

        class_paths = []
        class_tags = []
        categories_to_revalidate = set()

        for class_obj in business_classes:
            if hasattr(class_obj, "slug") and class_obj.slug:
                class_tags.append(f"class-{class_obj.slug}")

                # Also collect categories for bulk revalidation

        # Bulk revalidate all class tags
        if class_tags:
            all_tags_to_revalidate = (
                class_tags
                + list(categories_to_revalidate)
                + [
                    "classes-search",
                    "homepage-classes",
                    "businesses-list",
                    "businesses",
                    "public-businesses",
                ]
            )
            result = trigger_multiple_revalidations(tags=all_tags_to_revalidate)
            logger.info(
                f"Revalidated {result['successful']}/{result['total']} items for business {business.businessName} ({action_text})"
            )

        return Response(
            {"businessId": business.businessId, "featured": business.featured}
        )

    @action(detail=False, methods=["get"])
    def metrics(self, request):
        if not request.user.has_perm("quickstart.view_business_metrics"):
            self.permission_denied(
                request, message="You do not have permission to view business metrics."
            )

        window = get_admin_metrics_window(
            request.query_params, default_days=30, logger=logger
        )

        business_counts = BusinessInfo.objects.aggregate(
            total_businesses=Count("pk"),
            active_businesses=Count("pk", filter=Q(isActive=True)),
            featured_businesses=Count("pk", filter=Q(featured=True)),
            new_businesses_30d=Count(
                "pk",
                filter=Q(
                    createdAt__gte=window.start_dt,
                    createdAt__lt=window.end_dt_exclusive,
                ),
            ),
        )

        if window.all_time:
            total_business_growth = 0.0
        else:
            previous_period_businesses = BusinessInfo.objects.filter(
                createdAt__gte=window.previous_start_dt,
                createdAt__lt=window.previous_end_dt_exclusive,
            ).count()
            total_business_growth = 0
            if previous_period_businesses > 0:
                total_business_growth = (
                    (
                        business_counts["new_businesses_30d"]
                        - previous_period_businesses
                    )
                    / previous_period_businesses
                ) * 100

        active_businesses_in_period = count_engaged_businesses(
            window.start_dt, window.end_dt_exclusive
        )

        engaged_fixed_end = timezone.now()
        engaged_fixed_start = engaged_fixed_end - timedelta(
            days=ENGAGED_LOGIN_LOOKBACK_DAYS
        )
        active_engaged_last_30d = count_engaged_businesses(
            engaged_fixed_start, engaged_fixed_end
        )

        total_gross_revenue = Booking.objects.filter(
            status__in=["confirmed", "completed"],
            booking_date__gte=window.start_dt,
            booking_date__lt=window.end_dt_exclusive,
        ).aggregate(
            total=Coalesce(
                Sum("amount_paid"),
                Value(0),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )[
            "total"
        ]

        # Calculate total platform revenue by summing the fee AND the tax on the fee.
        total_platform_revenue_agg = Payment.objects.filter(
            status__in=["succeeded", "partially_refunded"],
            created_at__gte=window.start_dt,
            created_at__lt=window.end_dt_exclusive,
        ).aggregate(
            total=Coalesce(
                Sum(
                    F("platform_fee_amount") + F("platform_fee_tax")
                ),  # Use F() expression to sum both fields
                Value(Decimal("0.00")),  # Ensure default is Decimal
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        total_platform_revenue = total_platform_revenue_agg["total"]

        # Category distribution deprecated (ClassesMain no longer has category FK).
        category_distribution = []

        pie_palette = [
            "#3b82f6",
            "#8b5cf6",
            "#ec4899",
            "#10b981",
            "#f59e0b",
            "#ef4444",
            "#06b6d4",
            "#6366f1",
            "#84cc16",
            "#f97316",
        ]
        coll_qs = (
            ClassCollection.objects.filter(is_active=True)
            .annotate(class_count=Count("classes", distinct=True))
            .order_by("-class_count")[:15]
        )
        collection_distribution = [
            {
                "name": c.name,
                "value": c.class_count,
                "color": pie_palette[i % len(pie_palette)],
            }
            for i, c in enumerate(coll_qs)
        ]

        ca_codes = (
            "AB",
            "BC",
            "MB",
            "NB",
            "NL",
            "NS",
            "ON",
            "PE",
            "QC",
            "SK",
            "NT",
            "NU",
            "YT",
        )
        ca_set = set(ca_codes)
        count_by_prov = defaultdict(int)
        bids_by_prov = defaultdict(list)
        for bid, st in BusinessInfo.objects.values_list("businessId", "businessState"):
            if not st:
                continue
            code = self.get_province_code(st)
            if code not in ca_set:
                cand = str(st).strip().upper()[:2]
                code = cand if cand in ca_set else ""
            if not code:
                continue
            count_by_prov[code] += 1
            bids_by_prov[code].append(bid)

        province_distribution = []
        for code in ca_codes:
            bids = bids_by_prov.get(code, [])
            prov_rev = Decimal("0.00")
            if bids:
                prov_rev = (
                    Booking.objects.filter(
                        status__in=["confirmed", "completed"],
                        schedule_instance__schedule__option__classId__businessId__in=bids,
                    ).aggregate(
                        total=Coalesce(
                            Sum("amount_paid"),
                            Value(0),
                            output_field=DecimalField(max_digits=12, decimal_places=2),
                        )
                    )["total"]
                    or Decimal("0.00")
                )
            province_distribution.append(
                {
                    "province": code,
                    "count": count_by_prov.get(code, 0),
                    "revenue": float(prov_rev),
                }
            )

        monthly_growth_data = self._get_growth_data("month", 6)

        location_distribution_qs = (
            BusinessInfo.objects.values("businessCity", "businessState")
            .annotate(count=Count("pk"))
            .order_by("-count")[:12]
        )
        location_distribution = [
            {
                "city": loc["businessCity"],
                "state": loc["businessState"],
                "count": loc["count"],
                "region": self.get_region_for_province(loc["businessState"]),
            }
            for loc in location_distribution_qs
        ]

        top_cities = []
        city_rows = list(
            BusinessInfo.objects.exclude(businessCity__isnull=True)
            .exclude(businessCity="")
            .values("businessCity", "businessState")
            .annotate(count=Count("pk"))
            .order_by("-count")[:15]
        )
        for row in city_rows:
            bids = list(
                BusinessInfo.objects.filter(
                    businessCity=row["businessCity"],
                    businessState=row["businessState"],
                ).values_list("businessId", flat=True)
            )
            cls_n = ClassesMain.objects.filter(businessId__in=bids).count()
            city_rev = (
                Booking.objects.filter(
                    status__in=["confirmed", "completed"],
                    schedule_instance__schedule__option__classId__businessId__in=bids,
                ).aggregate(
                    total=Coalesce(
                        Sum("amount_paid"),
                        Value(0),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    )
                )["total"]
                or Decimal("0.00")
            )
            top_cities.append(
                {
                    "city": row["businessCity"],
                    "state": row["businessState"],
                    "count": row["count"],
                    "classes_count": cls_n,
                    "revenue": float(city_rev),
                    "region": self.get_region_for_province(row["businessState"]),
                }
            )

        preset_norm_keys = {
            _normalize_search_location_key(p) for p in EXPLORE_LOCATION_PRESET_LABELS
        }
        loc_counts = defaultdict(int)
        loc_display = {}
        for raw_loc in SearchLog.objects.exclude(location__isnull=True).exclude(
            location=""
        ).values_list("location", flat=True):
            s = (raw_loc or "").strip()
            if not s:
                continue
            k = _normalize_search_location_key(s)
            loc_counts[k] += 1
            if k not in loc_display or len(s) > len(loc_display[k]):
                loc_display[k] = s

        search_preset_demand = [
            {
                "label": preset_label,
                "count": loc_counts.get(_normalize_search_location_key(preset_label), 0),
            }
            for preset_label in EXPLORE_LOCATION_PRESET_LABELS
        ]
        custom_entries = []
        for k, cnt in loc_counts.items():
            if not k or k in preset_norm_keys:
                continue
            custom_entries.append(
                {"label": loc_display.get(k, k), "count": cnt}
            )
        custom_entries.sort(key=lambda x: -x["count"])
        search_custom_top = custom_entries[:10]

        search_location_top = [
            {
                "label": loc_display[k],
                "location": loc_display[k],
                "province": "",
                "count": loc_counts[k],
            }
            for k in sorted(loc_counts.keys(), key=lambda x: -loc_counts[x])[:10]
            if k
        ]

        top_businesses_queryset = self.filter_queryset(self.get_queryset()).order_by(
            "-revenue"
        )[:5]
        top_businesses_data = self.get_serializer(
            top_businesses_queryset, many=True
        ).data

        return Response(
            {
                "total_businesses": business_counts["total_businesses"],
                "active_platform_enabled_businesses": business_counts[
                    "active_businesses"
                ],
                "active_businesses": active_engaged_last_30d,
                "active_businesses_in_period": active_businesses_in_period,
                "total_business_growth": round(total_business_growth, 2),
                "featured_businesses": business_counts["featured_businesses"],
                "new_businesses_30d": business_counts["new_businesses_30d"],
                "total_revenue": float(total_gross_revenue),
                "total_platform_revenue": float(
                    total_platform_revenue
                ),  # This now uses the corrected value
                "category_distribution": category_distribution,
                "collection_distribution": collection_distribution,
                "province_distribution": province_distribution,
                "top_cities": top_cities,
                "search_location_top": search_location_top,
                "search_preset_demand": search_preset_demand,
                "search_custom_top": search_custom_top,
                "growth_trend": monthly_growth_data,
                "location_distribution": location_distribution,
                "top_businesses": top_businesses_data,
            }
        )

    def _log_business_action(self, business, action_code, details, request):
        try:
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action=action_code,
                details=details,
                target_user=business.owner,
                target_model="BusinessInfo",
                target_id=str(business.businessId),
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                metadata={
                    "business_name": business.businessName,
                    "business_id": business.businessId,
                    "owner_id": business.owner_id,
                },
            )
        except Exception as e:
            logger.error(
                f"Failed to create audit log for business action {action_code}: {str(e)}",
                exc_info=True,
            )

    @action(detail=False, methods=["get"])
    def export(self, request):
        if not request.user.has_perm("quickstart.export_business_data"):
            self.permission_denied(
                request, message="You do not have permission to export business data."
            )
        queryset = self.filter_queryset(self.get_queryset())
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="businesses_export.csv"'
        writer = csv.writer(response)
        # FIX: Removed the "Category" header as a business can have multiple categories.
        headers = [
            "Business ID",
            "Business Name",
            "Type",
            "City",
            "State",
            "Status",
            "Featured",
            "Avg Rating",
            "Reviews Count",
            "Bookings Count",
            "Classes Count",
            "Total Revenue",
            "Created At",
            "Owner Email",
        ]
        writer.writerow(headers)

        # FIX: Removed "classCategory__key" from values_list.
        business_data = queryset.values_list(
            "businessId",
            "businessName",
            "businessType",
            "businessCity",
            "businessState",
            "status",
            "featured",
            "rating",
            "review_count",
            "bookings_count",
            "classes_count",
            "revenue",
            "createdAt",
            "owner__email",
        )
        for business in business_data:
            # Adjust indices due to removed category column
            row = list(business)
            # Format boolean for 'Featured'
            row[6] = "Yes" if business[6] else "No"
            # Format numbers
            row[7] = round(business[7] or 0, 1)
            row[11] = float(business[11] or 0)
            # Format datetime
            row[12] = business[12].strftime("%Y-%m-%d %H:%M:%S") if business[12] else ""
            writer.writerow(row)
        return response

    def _get_growth_data(self, timeframe="month", periods=6):
        """Helper to calculate growth data for metrics and growth endpoint"""
        today = timezone.now().date()
        result = []
        trunc_func = TruncMonth

        if timeframe == "week":
            periods = 12
            period_length = 7
            trunc_func = TruncWeek
        elif timeframe == "month":
            periods = 6
            period_length = 30
            trunc_func = TruncMonth
        elif timeframe == "quarter":
            periods = 4
            period_length = 90
            trunc_func = TruncQuarter  # Approx
        else:  # year
            periods = 3
            period_length = 365
            trunc_func = TruncYear

        start_date_limit = today - timedelta(
            days=periods * period_length + period_length
        )  # Go back far enough

        # Aggregate businesses by period
        business_growth = (
            BusinessInfo.objects.filter(createdAt__date__gte=start_date_limit)
            .annotate(period=trunc_func("createdAt"))
            .values("period")
            .annotate(count=Count("pk"))
            .order_by("period")
        )

        # Aggregate revenue by period
        revenue_growth = (
            Booking.objects.filter(
                status__in=["confirmed", "completed"],
                booking_date__date__gte=start_date_limit,
            )
            .annotate(period=trunc_func("booking_date"))
            .values("period")
            .annotate(
                total_revenue=Coalesce(
                    Sum("amount_paid"), Value(0), output_field=DecimalField()
                )
            )
            .order_by("period")
        )

        # Combine data (simple join on period, assumes periods align)
        revenue_dict = {
            item["period"].strftime("%Y-%m-%d"): float(item["total_revenue"])
            for item in revenue_growth
            if item["period"]
        }
        business_dict = {
            item["period"].strftime("%Y-%m-%d"): item["count"]
            for item in business_growth
            if item["period"]
        }
        all_periods = sorted(list(set(revenue_dict.keys()) | set(business_dict.keys())))

        formatted_result = []
        for period_str in all_periods[-periods:]:  # Take the most recent 'periods'
            period_date = datetime.strptime(period_str, "%Y-%m-%d").date()
            # Format label based on timeframe
            if timeframe == "week":
                period_label = period_date.strftime("%d %b")  # Start of week
            elif timeframe == "month":
                period_label = period_date.strftime("%b %Y")
            elif timeframe == "quarter":
                quarter = ((period_date.month - 1) // 3) + 1
                period_label = f"Q{quarter} {period_date.year}"
            else:
                period_label = str(period_date.year)

            formatted_result.append(
                {
                    "month": period_label,  # Keep 'month' key for frontend compatibility?
                    "period_start": period_str,
                    "businesses": business_dict.get(period_str, 0),
                    "revenue": revenue_dict.get(period_str, 0.0),
                }
            )
        return formatted_result

    @action(detail=False, methods=["get"])
    def growth(self, request):
        """Get growth trends based on timeframe"""
        if not request.user.has_perm("quickstart.view_business_metrics"):
            self.permission_denied(
                request,
                message="You do not have permission to view business growth metrics.",
            )

        timeframe = request.query_params.get("timeframe", "month")
        result = self._get_growth_data(timeframe)
        return Response(result)

    @action(detail=False, methods=["get"])
    def geographical(self, request):
        """Get geographical distribution of businesses, aggregated by CITY, using subqueries."""
        if not request.user.has_perm("quickstart.view_business_metrics"):
            self.permission_denied(
                request,
                message="You do not have permission to view geographical business data.",
            )

        data_type = request.query_params.get("data_type", "count")

        # --- Define Subqueries for City-Level Aggregation ---

        # Subquery for business count
        count_subquery = Subquery(
            BusinessInfo.objects.filter(
                businessCity=OuterRef("businessCity"),
                businessState=OuterRef("businessState"),
            )
            .values("businessCity", "businessState")
            .annotate(c=Count("pk"))
            .values("c"),
            output_field=IntegerField(),
        )

        # Subquery for total revenue
        revenue_subquery = Subquery(
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId__businessCity=OuterRef(
                    "businessCity"
                ),
                schedule_instance__schedule__option__classId__businessId__businessState=OuterRef(
                    "businessState"
                ),
                status__in=["confirmed", "completed"],
            )
            .values(
                "schedule_instance__schedule__option__classId__businessId__businessCity",
                "schedule_instance__schedule__option__classId__businessId__businessState",
            )
            .annotate(s=Sum("amount_paid"))
            .values("s"),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )

        # Subquery for total classes count
        classes_subquery = Subquery(
            ClassesMain.objects.filter(
                businessId__businessCity=OuterRef("businessCity"),
                businessId__businessState=OuterRef("businessState"),
            )
            .values("businessId__businessCity", "businessId__businessState")
            .annotate(c=Count("pk"))
            .values("c"),
            output_field=IntegerField(),
        )

        # --- NEW: Subqueries for Average Lat/Lon ---
        avg_lat_subquery = Subquery(
            BusinessInfo.objects.filter(
                businessCity=OuterRef("businessCity"),
                businessState=OuterRef("businessState"),
                latitude__isnull=False,  # Ensure we only average non-nulls
            )
            .values("businessCity", "businessState")  # Grouping matches outer distinct
            .annotate(avg=Avg("latitude"))  # Calculate average
            .values("avg"),
            output_field=FloatField(),
        )

        avg_lon_subquery = Subquery(
            BusinessInfo.objects.filter(
                businessCity=OuterRef("businessCity"),
                businessState=OuterRef("businessState"),
                longitude__isnull=False,  # Ensure we only average non-nulls
            )
            .values("businessCity", "businessState")  # Grouping matches outer distinct
            .annotate(avg=Avg("longitude"))  # Calculate average
            .values("avg"),
            output_field=FloatField(),
        )

        # --- Main Query ---
        # Start with distinct city/state combinations (no coordinate filter needed here)
        queryset = (
            BusinessInfo.objects.values("businessCity", "businessState")
            .distinct()
            .annotate(  # Annotate onto the distinct city/state pairs
                # Apply all subqueries
                avg_lat=avg_lat_subquery,  # Use the subquery result
                avg_lon=avg_lon_subquery,  # Use the subquery result
                city_count=Coalesce(count_subquery, 0),
                city_revenue=Coalesce(revenue_subquery, Value(Decimal("0.00"))),
                city_classes_count=Coalesce(classes_subquery, 0),
            )
            .filter(
                # --- Filter AFTER annotation: Ensure we only keep cities with valid coordinates ---
                avg_lat__isnull=False,
                avg_lon__isnull=False,
            )
            .order_by("-city_count")
        )  # Order by the calculated count

        result_data = []
        for item in queryset:
            # Extract values (now we know avg_lat/lon should exist if not filtered out)
            city = item.get("businessCity")
            province_name = item.get("businessState")
            avg_lat_val = item.get("avg_lat")  # Directly use the annotated value
            avg_lon_val = item.get("avg_lon")  # Directly use the annotated value
            count_val = item.get("city_count", 0)
            revenue_val = item.get("city_revenue", Decimal("0.00"))
            classes_count_val = item.get("city_classes_count", 0)

            # Minimal validation needed here now, as None values were filtered
            if (
                city
                and province_name
                and avg_lat_val is not None
                and avg_lon_val is not None
            ):
                try:
                    centroid_lon = float(avg_lon_val)
                    centroid_lat = float(avg_lat_val)
                except (ValueError, TypeError):
                    logger.error(
                        f"Unexpected non-float coordinate for {city}, {province_name}: Lat={avg_lat_val}, Lon={avg_lon_val}"
                    )
                    continue  # Should not happen due to filter, but safety check

                province_code = self.get_province_code(province_name)
                region = self.get_region_for_province(province_name)

                result_data.append(
                    {
                        "city": city,
                        "province_code": province_code,
                        "province_full": province_name,
                        "region": region,
                        "count": count_val,
                        "classes_count": classes_count_val,  # Include class count
                        "revenue": float(revenue_val),
                        "growth": 0,
                        "centroid": [centroid_lon, centroid_lat],
                    }
                )
            # No else needed as filtering should prevent this

        # Sorting and limiting remain the same
        sort_key = "count"
        if data_type == "revenue":
            sort_key = "revenue"
        result_data.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
        limit = 50
        result_data = result_data[:limit]

        return Response(result_data)

    @action(detail=False, methods=["post"])
    def announcements(self, request):
        """Send announcements to businesses"""
        if not request.user.has_perm("quickstart.send_business_announcements"):
            self.permission_denied(
                request, message="You do not have permission to send announcements."
            )

        # --- Input validation ---
        recipient_type = request.data.get("recipientType", "all")
        title = request.data.get("title")
        message = request.data.get("message")
        urgency = request.data.get("urgency", "normal")  # Consider using this
        send_email = request.data.get("sendEmail", True)
        send_in_app = request.data.get("sendInApp", True)  # Consider implementation

        if not title or not message:
            return Response(
                {"error": "Title and message are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- Select recipient businesses ---
        businesses_qs = BusinessInfo.objects.all()  # Start with all

        if recipient_type == "active":
            businesses_qs = businesses_qs.filter(isActive=True)
        elif recipient_type == "featured":
            businesses_qs = businesses_qs.filter(featured=True)
        elif recipient_type == "new":
            thirty_days_ago = timezone.now() - timedelta(days=30)
            businesses_qs = businesses_qs.filter(createdAt__gte=thirty_days_ago)
        elif recipient_type == "verified":
            businesses_qs = businesses_qs.filter(verificationStatus="verified")
        # Add more types if needed (e.g., specific category)

        # Get distinct owner emails efficiently
        owner_emails = list(
            businesses_qs.filter(owner__isnull=False, owner__email__isnull=False)
            .values_list("owner__email", flat=True)
            .distinct()
        )

        # --- Actual implementation would involve background tasks ---
        if not owner_emails:
            return Response(
                {
                    "success": True,  # Or False? Depends on expectation
                    "message": "No recipients found for the selected criteria.",
                    "recipient_count": 0,
                }
            )

        logger.info(
            f"Announcement '{title}' triggered for {len(owner_emails)} business owners by Admin {request.user.email}"
        )

        # Example: Use Celery or Django-Q
        # from your_tasks import send_announcement_task
        # send_announcement_task.delay(owner_emails, title, message, send_email, send_in_app)

        # --- Log Action ---
        # Optionally create an AuditLog entry for sending announcements

        return Response(
            {
                "success": True,
                "message": f"Announcement task queued for {len(owner_emails)} recipients.",
                "recipient_count": len(owner_emails),
                "sent_emails": send_email,
                "sent_in_app": send_in_app,  # Reflects intent, not completion
            }
        )

    # --- Helper methods ---
    def get_province_code(self, province_name):
        """Map province full name to code. Case-insensitive."""
        if not province_name:
            return ""  # Handle None or empty string
        province_map = {
            "ontario": "ON",
            "quebec": "QC",
            "british columbia": "BC",
            "alberta": "AB",
            "manitoba": "MB",
            "saskatchewan": "SK",
            "nova scotia": "NS",
            "new brunswick": "NB",
            "newfoundland and labrador": "NL",
            "prince edward island": "PE",
            "northwest territories": "NT",
            "yukon": "YT",
            "nunavut": "NU",
        }
        # Also map codes to themselves for safety
        code_map = {v: v for v in province_map.values()}
        province_map.update(code_map)

        cleaned_name = str(province_name).strip().lower()
        return province_map.get(
            cleaned_name, cleaned_name[:2].upper()
        )  # Default to first 2 chars

    def get_region_for_province(self, province):
        """Map province (name or code) to region. Case-insensitive."""
        if not province:
            return "Other"  # Handle None or empty string
        # Map both full names and codes
        region_map = {
            # Full Names (lowercase)
            "ontario": "Central",
            "quebec": "Eastern",
            "british columbia": "Western",
            "alberta": "Western",
            "manitoba": "Central",
            "saskatchewan": "Central",
            "nova scotia": "Atlantic",
            "new brunswick": "Atlantic",
            "newfoundland and labrador": "Atlantic",
            "prince edward island": "Atlantic",
            "northwest territories": "Northern",
            "yukon": "Northern",
            "nunavut": "Northern",
            # Codes (lowercase)
            "on": "Central",
            "qc": "Eastern",
            "bc": "Western",
            "ab": "Western",
            "mb": "Central",
            "sk": "Central",
            "ns": "Atlantic",
            "nb": "Atlantic",
            "nl": "Atlantic",
            "pe": "Atlantic",
            "nt": "Northern",
            "yt": "Northern",
            "nu": "Northern",
        }
        cleaned_province = str(province).strip().lower()
        return region_map.get(cleaned_province, "Other")


class AdminGeographicalDataView(generics.ListAPIView):
    """
    Provides aggregated geographical data for the Canadian distribution map,
    powered by PostGIS.

    Supports two modes via query parameter `view`:
    - `choropleth` (default): Aggregates data by province boundary.
    - `bubble`: Aggregates data by city, providing a centroid for mapping.
    """

    permission_classes = [IsAuthenticated, CanAccessClassAdmin]
    serializer_class = GeographicBoundaryDataSerializer

    def get_queryset_for_choropleth(self):
        """
        Groups all businesses by the province they fall into.
        This query is faster and suitable for coloring entire provinces.
        """
        # Subquery to get total revenue for each business
        business_revenue = BusinessInfo.objects.annotate(
            revenue=Coalesce(
                Sum(
                    "classes__options__schedules__instances__bookings__payments__amount",
                    filter=Q(
                        classes__options__schedules__instances__bookings__payments__status="succeeded"
                    ),
                ),
                Decimal("0.0"),
            )
        ).values("businessId", "revenue")

        # We can't do a direct spatial join and group by province name easily with the ORM.
        # Instead, we'll aggregate by the province string field on BusinessInfo, which is efficient.
        province_aggregation = (
            BusinessInfo.objects.filter(
                isActive=True, latitude__isnull=False, longitude__isnull=False
            )
            .values("businessState")
            .annotate(
                province_full=F("businessState"),
                count=Count("businessId"),
                revenue=Sum(
                    "bookings__payments__amount",
                    filter=Q(bookings__payments__status="succeeded"),
                ),
                classes_count=Count("classes", distinct=True),
            )
            .order_by("businessState")
        )

        return province_aggregation

    def get_queryset_for_bubble(self):
        """
        Groups businesses by city and calculates a geographic centroid for each city.
        This is used to place bubbles on the map.
        """
        city_aggregation = (
            BusinessInfo.objects.filter(
                isActive=True, latitude__isnull=False, longitude__isnull=False
            )
            .values("businessCity", "businessState")
            .annotate(
                city=F("businessCity"),
                province_full=F("businessState"),
                count=Count("businessId"),
                revenue=Coalesce(
                    Sum(
                        "bookings__payments__amount",
                        filter=Q(bookings__payments__status="succeeded"),
                    ),
                    Decimal("0.0"),
                ),
                classes_count=Count("classes", distinct=True),
                # PostGIS Function: Calculate the center point of all businesses in the city
                centroid_coords=Centroid(F("point")),
            )
            .order_by("-count")
        )

        return city_aggregation

    def list(self, request, *args, **kwargs):
        # The frontend needs BOTH datasets: province totals for the choropleth
        # and city-specific points for the bubbles. We'll return them together.

        choropleth_data = list(self.get_queryset_for_choropleth())
        bubble_data = list(self.get_queryset_for_bubble())

        # Convert GIS Point object in bubble_data to a simple [lon, lat] list
        for item in bubble_data:
            if item.get("centroid_coords"):
                point = item["centroid_coords"]
                item["centroid"] = [point.x, point.y]
                del item["centroid_coords"]  # Clean up the response
            else:
                item["centroid"] = None

        return Response({"province_data": choropleth_data, "city_data": bubble_data})


# ---------------------------------------------------------------------------
# Import Google Reviews (admin: upload CSV/JSON and run import_google_reviews)
# ---------------------------------------------------------------------------

import json
from io import StringIO

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser

from quickstart.services.google_reviews_importer import import_reviews_for_business


def _csv_row_to_review(raw):
    """Map a CSV row (dict with normalized keys) to import_google_reviews JSON shape."""
    review_image_urls = raw.get("reviewimageurls") or raw.get("review_image_urls") or ""
    if isinstance(review_image_urls, str) and review_image_urls.strip():
        review_image_urls = [u.strip() for u in review_image_urls.replace("|", ",").split(",") if u.strip()]
    else:
        review_image_urls = []
    return {
        "reviewId": (raw.get("reviewid") or raw.get("review_id") or "").strip(),
        "name": (raw.get("name") or "").strip() or "Anonymous",
        "stars": int(raw.get("stars") or raw.get("rating") or 0),
        "text": (raw.get("text") or raw.get("comment") or "").strip(),
        "publishedAtDate": (raw.get("publishedatdate") or raw.get("published_at_date") or "").strip() or None,
        "responseFromOwnerText": (raw.get("responsefromownertext") or raw.get("response_from_owner_text") or "").strip() or None,
        "responseFromOwnerDate": (raw.get("responsefromownerdate") or raw.get("response_from_owner_date") or "").strip() or None,
        "reviewerPhotoUrl": (raw.get("reviewerphotourl") or raw.get("reviewer_photo_url") or "").strip() or None,
        "reviewImageUrls": review_image_urls,
    }


def _parse_csv_to_reviews(content):
    """Parse CSV content (string) into list of review dicts for import_google_reviews."""
    reader = csv.DictReader(StringIO(content))
    rows = list(reader)
    if not rows:
        return []
    # Normalize headers to lowercase for flexible matching
    result = []
    for row in rows:
        normalized = {k.strip().lower().replace(" ", ""): (v or "").strip() for k, v in row.items()}
        result.append(_csv_row_to_review(normalized))
    return result


class ImportGoogleReviewsAdminView(APIView):
    """
    Admin-only: upload a CSV or JSON file and run import_google_reviews for a business.
    POST multipart: business_id (int), file (CSV or JSON).
    """
    permission_classes = [IsAuthenticated, CanAccessBusinessAdmin]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        business_id = request.data.get("business_id")
        if business_id is None:
            return Response(
                {"error": "Missing business_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            business_id = int(business_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "business_id must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        upload = request.FILES.get("file")
        if not upload:
            return Response(
                {"error": "Missing file. Upload a CSV or JSON file."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        name = (upload.name or "").lower()
        is_csv = name.endswith(".csv")
        is_json = name.endswith(".json")

        if not is_csv and not is_json:
            return Response(
                {"error": "File must be .csv or .json."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            content = upload.read().decode("utf-8")
        except UnicodeDecodeError:
            return Response(
                {"error": "File must be UTF-8 encoded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reviews_data = None
        if is_json:
            try:
                reviews_data = json.loads(content)
            except json.JSONDecodeError as e:
                return Response(
                    {"error": f"Invalid JSON: {e}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            reviews_data = _parse_csv_to_reviews(content)

        if not isinstance(reviews_data, list):
            return Response(
                {"error": "JSON file must be a list of review objects."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            business = BusinessInfo.objects.get(businessId=business_id)
        except BusinessInfo.DoesNotExist:
            return Response(
                {"error": "Business not found.", "success": False},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            stats = import_reviews_for_business(
                business,
                reviews_data,
                skip_images=False,
                full_refresh=True,
            )
            return Response(
                {
                    "success": True,
                    "message": "Import completed.",
                    "stats": stats,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Import Google reviews failed")
            return Response(
                {"error": str(e), "success": False},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class AdminBusinessGoogleReviewsSyncView(APIView):
    """
    Admin-only: enqueue Apify Google reviews sync for a business (uses google_maps_url).
    """

    permission_classes = [IsAuthenticated, CanAccessBusinessAdmin]

    def post(self, request, business_id):
        try:
            bid = int(business_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "Invalid business_id.", "success": False},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not BusinessInfo.objects.filter(businessId=bid).exists():
            return Response(
                {"error": "Business not found.", "success": False},
                status=status.HTTP_404_NOT_FOUND,
            )
        from quickstart.tasks.google_reviews_tasks import (
            sync_google_reviews_for_business,
        )

        sync_google_reviews_for_business.delay(bid)
        return Response({"success": True, "queued": True}, status=status.HTTP_200_OK)


class AdminGoogleReviewsSyncQueueView(APIView):
    """
    Admin-only: queue Google reviews Apify sync for all businesses with a Maps URL,
    or for a single business (by business_id).
    POST JSON: { "all": true } or { "business_id": 123 }
    """

    permission_classes = [IsAuthenticated, CanAccessBusinessAdmin]

    def post(self, request):
        from quickstart.tasks.google_reviews_tasks import (
            sync_google_reviews_enqueue_all,
            sync_google_reviews_for_business,
        )

        all_flag = request.data.get("all") is True
        business_id = request.data.get("business_id")

        if all_flag:
            sync_google_reviews_enqueue_all.delay()
            return Response(
                {
                    "success": True,
                    "queued": "all",
                    "message": "Queued Google reviews sync for all businesses with a Google Maps URL.",
                },
                status=status.HTTP_200_OK,
            )

        if business_id is not None and business_id != "":
            try:
                bid = int(business_id)
            except (TypeError, ValueError):
                return Response(
                    {"error": "business_id must be an integer.", "success": False},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not BusinessInfo.objects.filter(businessId=bid).exists():
                return Response(
                    {"error": "Business not found.", "success": False},
                    status=status.HTTP_404_NOT_FOUND,
                )
            sync_google_reviews_for_business.delay(bid)
            return Response(
                {
                    "success": True,
                    "queued": "business",
                    "business_id": bid,
                    "message": f"Queued Google reviews sync for business {bid}.",
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "error": "Provide all: true or business_id (integer).",
                "success": False,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )


class AdminInstagramFollowersSyncQueueView(APIView):
    """
    Admin-only: queue Instagram follower Apify sync for all businesses with an
    Instagram link, or for a single business.
    POST JSON: { "all": true } or { "business_id": 123 }
    """

    permission_classes = [IsAuthenticated, CanAccessBusinessAdmin]

    def post(self, request):
        from quickstart.tasks.instagram_tasks import (
            sync_instagram_followers_enqueue_all,
            sync_instagram_followers_for_business,
        )

        all_flag = request.data.get("all") is True
        business_id = request.data.get("business_id")

        if all_flag:
            sync_instagram_followers_enqueue_all.delay()
            return Response(
                {
                    "success": True,
                    "queued": "all",
                    "message": "Queued Instagram follower sync for all businesses with an Instagram URL.",
                },
                status=status.HTTP_200_OK,
            )

        if business_id is not None and business_id != "":
            try:
                bid = int(business_id)
            except (TypeError, ValueError):
                return Response(
                    {"error": "business_id must be an integer.", "success": False},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not BusinessInfo.objects.filter(businessId=bid).exists():
                return Response(
                    {"error": "Business not found.", "success": False},
                    status=status.HTTP_404_NOT_FOUND,
                )
            sync_instagram_followers_for_business.delay(bid)
            return Response(
                {
                    "success": True,
                    "queued": "business",
                    "business_id": bid,
                    "message": f"Queued Instagram follower sync for business {bid}.",
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "error": "Provide all: true or business_id (integer).",
                "success": False,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
