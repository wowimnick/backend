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
    Contact,
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


class BusinessStudentViewSet(
    viewsets.ModelViewSet
):  # CHANGED to ModelViewSet for destroy
    serializer_class = BusinessStudentProfileSerializer
    permission_classes = [IsAuthenticated, CanViewBusinessStudents]
    pagination_class = StudentPagination
    search_fields = [
        "first_name",
        "last_name",
        "email",
        "phone_number",
        "user__first_name",
        "user__last_name",
        "user__email",
    ]
    ordering_fields = [
        "first_name",
        "last_name",
        "email",
        "last_booking_date_this_business",
        "total_spent_this_business",
    ]
    ordering = ["last_name", "first_name"]
    # ALLOWED `destroy` method
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_business_context(self):
        user = self.request.user
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            raise PermissionDenied("User not associated with any managed business.")
        return business

    def get_queryset(self):
        business = self.get_business_context()

        # Base queryset is now Contact
        queryset = Contact.objects.filter(business=business).select_related(
            "user", "user__role"
        )

        # Annotations now pull data from the linked user, coalescing to 0/null if no user is linked
        thirty_days_ago_date = (timezone.now() - timezone.timedelta(days=30)).date()

        # Subquery for first paid booking in a course group
        first_paid_course_booking_id_subquery = Subquery(
            Booking.objects.filter(
                user_id=OuterRef("user_id"),
                booking_group_id=OuterRef("user__bookings__booking_group_id"),
                payment_status="paid",
            )
            .order_by("booking_date", "id")
            .values("id")[:1]
        )

        queryset = queryset.annotate(
            is_active=Exists(
                Booking.objects.filter(
                    user_id=OuterRef("user_id"),
                    status="confirmed",
                    schedule_instance__date__gte=thirty_days_ago_date,
                    schedule_instance__schedule__option__classId__businessId=business.businessId,
                )
            ),
            last_booking_datetime=Max(
                "user__bookings__booking_date",
                filter=Q(
                    user__bookings__schedule_instance__schedule__option__classId__businessId=business.businessId
                ),
            ),
            total_classes_taken=Coalesce(
                Count(
                    "user__bookings",
                    filter=Q(
                        user__bookings__status="completed",
                        user__bookings__schedule_instance__schedule__option__classId__businessId=business.businessId,
                    ),
                ),
                Value(0),
            ),
            total_spent_this_business=Coalesce(
                Sum(
                    "user__bookings__amount_paid",
                    filter=Q(
                        user__bookings__payment_status="paid",
                        user__bookings__schedule_instance__schedule__option__classId__businessId=business.businessId,
                    )
                    & (
                        Q(user__bookings__booking_group_id__isnull=True)
                        | Q(user__bookings__id=first_paid_course_booking_id_subquery)
                    ),
                ),
                Value(Decimal("0.0")),
                output_field=DecimalField(),
            ),
        ).annotate(
            last_booking_date_this_business=ExpressionWrapper(
                TruncDate(F("last_booking_datetime")),
                output_field=DateField(),
            )
        )

        # Apply status filter
        status_filter_param = self.request.query_params.get("status_filter", "all")
        if status_filter_param == "active":
            queryset = queryset.filter(user__isnull=False)
        elif status_filter_param == "inactive":
            queryset = queryset.filter(user__isnull=True)

        # Prefetch notes
        user = self.request.user
        if user.has_perm("quickstart.view_studentnote"):
            notes_qs = (
                StudentNote.objects.filter(business=business)
                .select_related("author")
                .order_by("-created_at")
            )
            queryset = queryset.prefetch_related(Prefetch("notes", queryset=notes_qs))

        return queryset.distinct()

    def destroy(self, request, *args, **kwargs):
        """
        Deletes an imported contact record.
        """
        contact = self.get_object()

        # Safety Check 1: Do not delete if the contact is a platform user
        if contact.user:
            return Response(
                {
                    "detail": "Cannot delete a contact that is linked to a platform user account."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Safety Check 2: Do not delete if there are appointments associated
        if contact.appointments.exists():
            return Response(
                {
                    "detail": "Cannot delete a contact with scheduled appointments. Please cancel or reassign them first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        contact_name = f"{contact.first_name} {contact.last_name}".strip()
        logger.info(
            f"User {request.user.email} is deleting contact '{contact_name}' (ID: {contact.id})."
        )

        self.perform_destroy(contact)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanManageBusinessStudentNotes],
    )
    def add_note(self, request, pk=None):
        """
        Adds a note to a contact or a user via their contact record.
        """
        contact = self.get_object()  # This now gets the Contact instance
        if not request.user.has_perm("quickstart.add_studentnote"):
            raise PermissionDenied("You do not have permission to add notes.")

        business = self.get_business_context()
        serializer = BusinessStudentNoteSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        # The 'content_object' will be the Contact instance
        note = serializer.save(
            content_object=contact, business=business, author=request.user
        )

        logger.info(
            f"Note added for Contact {contact.id} by {request.user.email} in business {business.businessName}"
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    # The retrieve method is largely handled by the serializer now,
    # but we can add booking history for platform users.
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()  # This is a Contact instance

        # For platform users, we enrich with booking history
        if instance.user:
            business = self.get_business_context()
            booking_history_qs = (
                Booking.objects.filter(
                    user=instance.user,
                    schedule_instance__schedule__option__classId__businessId=business,
                )
                .select_related(
                    "schedule_instance__schedule__option__classId",
                )
                .order_by("-schedule_instance__date", "-schedule_instance__time")[:50]
            )
            # Attach for the serializer to pick up
            instance.booking_history = booking_history_qs
        else:
            instance.booking_history = []

        serializer = self.get_serializer(instance)
        return Response(serializer.data)
