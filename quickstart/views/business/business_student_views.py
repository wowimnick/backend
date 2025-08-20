# quickstart/views/business/business_student_views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from django.db.models import (
    Prefetch,
    Q,
    Count,
    Value,
    Case,
    DecimalField,
    When,
    F,
    ExpressionWrapper,
    Exists,
    Subquery,
    OuterRef,
    IntegerField,
    Max,
    Sum,
    DateField,
    DateTimeField,  # Added DateTimeField
)
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from rest_framework.exceptions import (
    PermissionDenied,
    ValidationError as DRFValidationError,
    NotFound,
)
from decimal import Decimal
from rest_framework.pagination import PageNumberPagination

from quickstart.models import (
    Booking,
    BusinessInfo,
    CustomUser,
    StudentNote,
    ScheduleInstance,
    ClassesMain,
    ClassOption,
)  # Added missing
from quickstart.serializers.business.business_student_serializers import (
    BusinessStudentProfileSerializer,
    BusinessStudentNoteSerializer,
    BookingHistorySerializer,
)
import logging

logger = logging.getLogger(__name__)


# --- Permissions (Keep as is) ---
class CanViewBusinessStudents(BasePermission):
    message = "You do not have permission to view students for this business."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        # MODIFIED: This logic now correctly checks for staff membership.
        # It ensures the user not only has the permission but is also a member of a business.
        has_permission_codename = user.has_perm("quickstart.view_business_students")
        is_business_member = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).exists()

        return has_permission_codename and is_business_member


class CanManageBusinessStudentNotes(BasePermission):
    message = "You do not have permission to manage notes for this student."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        # This check is fine as it only relies on the permission codename.
        return user.has_perm("quickstart.view_studentnote") or user.has_perm(
            "quickstart.add_studentnote"
        )


# --- Pagination Class (Keep as is) ---
class StudentPagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = "page_size"
    max_page_size = 48


class BusinessStudentViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BusinessStudentProfileSerializer
    permission_classes = [IsAuthenticated, CanViewBusinessStudents]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    pagination_class = StudentPagination
    search_fields = ["email", "first_name", "last_name", "phone_number", "userId"]
    ordering_fields = [
        "first_name",
        "last_name",
        "email",
        "active_bookings_count",
        "completed_bookings_count",
        "last_booking_date_this_business",
        "total_spent_this_business",
        "bookings__user_facing_reference",
    ]
    ordering = ["last_name", "first_name"]
    http_method_names = ["get", "post", "head", "options"]

    def get_business_context(self):
        user = self.request.user
        try:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()
            if not business:
                raise PermissionDenied("User not associated with any managed business.")
            return business
        except BusinessInfo.DoesNotExist:
            raise PermissionDenied("Associated business not found unexpectedly.")

    def get_queryset(self):
        user = self.request.user
        business = self.get_business_context()

        queryset = (
            CustomUser.objects.filter(
                bookings__schedule_instance__schedule__option__classId__businessId=business
            )
            .select_related("role")
            .distinct()
        )

        business_filter_q = Q(
            bookings__schedule_instance__schedule__option__classId__businessId=business
        )
        thirty_days_ago_date = (timezone.now() - timezone.timedelta(days=30)).date()

        first_paid_course_booking_id_subquery = Subquery(
            Booking.objects.filter(
                booking_group_id=OuterRef("bookings__booking_group_id"),
                schedule_instance__schedule__option__classId__businessId=business,
                payment_status="paid",
            )
            .order_by("booking_date", "id")
            .values("id")[:1]
        )

        queryset = queryset.annotate(
            active_bookings_count=Count(
                "bookings", filter=Q(bookings__status="confirmed") & business_filter_q
            ),
            completed_bookings_count=Count(
                "bookings", filter=Q(bookings__status="completed") & business_filter_q
            ),
            is_active_student=Exists(
                Booking.objects.filter(
                    user=OuterRef("pk"),
                    status="confirmed",
                    schedule_instance__date__gte=thirty_days_ago_date,
                    schedule_instance__schedule__option__classId__businessId=business,
                )
            ),
            last_booking_datetime_this_business=Max(
                "bookings__booking_date", filter=business_filter_q
            ),
            total_spent_this_business=Coalesce(
                Sum(
                    "bookings__amount_paid",
                    filter=business_filter_q
                    & Q(bookings__payment_status="paid")
                    & (
                        Q(bookings__booking_group_id__isnull=True)
                        | Q(bookings__id=first_paid_course_booking_id_subquery)
                    ),
                ),
                Value(Decimal("0.0")),
                output_field=DecimalField(),
            ),
        ).annotate(
            last_booking_date_this_business=ExpressionWrapper(
                TruncDate(F("last_booking_datetime_this_business")),
                output_field=DateField(),
            ),
        )

        status_filter_param = self.request.query_params.get("status_filter", "all")
        if status_filter_param == "active":
            queryset = queryset.filter(is_active_student=True)
        elif status_filter_param == "inactive":
            queryset = queryset.filter(is_active_student=False)

        can_view_notes = user.has_perm("quickstart.view_studentnote")
        if can_view_notes:
            notes_for_this_business_qs = (
                StudentNote.objects.filter(business=business)
                .select_related("author")
                .order_by("-created_at")
            )
            queryset = queryset.prefetch_related(
                Prefetch(
                    "business_notes",
                    queryset=notes_for_this_business_qs,
                    to_attr="notes_for_this_business",
                )
            )
        return queryset

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        business = self.get_business_context()

        # Fetch booking history for this student within this business context
        booking_history_qs = (
            Booking.objects.filter(
                user=instance,
                schedule_instance__schedule__option__classId__businessId=business,
            )
            .select_related(
                "schedule_instance__schedule__option__classId",
                "schedule_instance__schedule__option",
            )
            .order_by("-schedule_instance__date", "-schedule_instance__time")[:50]
        )

        # Attach the data to the instance for the serializer
        instance.booking_history = booking_history_qs

        serializer = self.get_serializer(instance, context={"request": request})
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanManageBusinessStudentNotes],
    )
    def add_note(self, request, pk=None):
        student_user = self.get_object()
        if not request.user.has_perm("quickstart.add_studentnote"):
            raise PermissionDenied("You do not have permission to add notes.")
        business = self.get_business_context()
        serializer = BusinessStudentNoteSerializer(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save(user=student_user, business=business, author=request.user)
            logger.info(
                f"Note added for student {student_user.email} (ID: {pk}) by {request.user.email} in business {business.businessName}"
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        else:
            logger.warning(
                f"Failed to add note for student {student_user.email} by {request.user.email}. Errors: {serializer.errors}"
            )
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
