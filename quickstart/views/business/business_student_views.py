from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from django.db.models import Prefetch, Q, Count, Value, Case, DecimalField, When, F, ExpressionWrapper, Exists, Subquery, OuterRef, IntegerField
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from decimal import Decimal # Import Decimal

# Adjust imports based on final structure
from ...models import Booking, BusinessInfo, CustomUser, StudentNote
from ...serializers.business.business_student_serializers import ( # Updated path
    BusinessStudentProfileSerializer, BusinessStudentNoteSerializer
)

import logging
logger = logging.getLogger(__name__)

# --- Permissions ---

class CanViewBusinessStudents(BasePermission):
     """Checks if user has 'view_business_students' permission and is associated with a business."""
     message = "You do not have permission to view students for this business."

     def has_permission(self, request, view):
         user = request.user
         if not user or not user.is_authenticated: return False
         # Check permission AND business association
         return (
              user.has_perm('quickstart.view_business_students') and
              BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).exists()
         )
     # No object permission needed here as filtering is done in get_queryset

class CanManageBusinessStudentNotes(BasePermission):
     """ Checks 'add_studentnote' or 'view_studentnote' permission. Object check done in view/action."""
     message = "You do not have permission to manage notes for this student."

     def has_permission(self, request, view):
         user = request.user
         if not user or not user.is_authenticated: return False
         # Check if user has either view or add permission
         return (
              user.has_perm('quickstart.view_studentnote') or
              user.has_perm('quickstart.add_studentnote')
         )
     # Object-level check (is student associated with *this* business?) done within the add_note action

# --- ViewSet ---

class BusinessStudentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for Business Users viewing Students associated with their business.
    """
    serializer_class = BusinessStudentProfileSerializer # Use the renamed business serializer
    permission_classes = [IsAuthenticated, CanViewBusinessStudents] # Use the specific business permission
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['email', 'first_name', 'last_name', 'phone_number', 'userId']
    ordering_fields = ['first_name', 'last_name', 'email', 'active_bookings_count', 'completed_bookings_count', 'average_attendance'] # Match annotation names
    ordering = ['last_name', 'first_name']

    # Allow GET (list, retrieve) and POST (for add_note action)
    http_method_names = ['get', 'post', 'head', 'options']

    def get_business_context(self):
        """Helper to get the business associated with the request user."""
        user = self.request.user
        try:
            business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
            if not business:
                 raise PermissionDenied("User not associated with any managed business.")
            return business
        except BusinessInfo.DoesNotExist: # Should be caught by filter().first() but good practice
             raise PermissionDenied("Associated business not found unexpectedly.")

    def get_queryset(self):
        user = self.request.user
        business = self.get_business_context()

        queryset = CustomUser.objects.filter(
            bookings__schedule_instance__schedule__option__classId__businessId=business
        ).select_related('role').distinct()

        business_filter_q = Q(bookings__schedule_instance__schedule__option__classId__businessId=business)
        thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
        active_booking_subquery = Booking.objects.filter(
            user=OuterRef('pk'),
            status='confirmed',
            schedule_instance__date__gte=thirty_days_ago.date(),
            schedule_instance__schedule__option__classId__businessId=business
        )
        queryset = queryset.annotate(
             active_bookings_count=Count(
                'bookings', filter=Q(bookings__status='confirmed') & business_filter_q
            ),
            completed_bookings_count=Count(
                'bookings', filter=Q(bookings__status='completed') & business_filter_q
            ),
            total_finished_bookings_for_rate=Count(
                'bookings', filter=Q(bookings__status__in=['completed', 'cancelled']) & business_filter_q
            ),
            is_active_student=Exists(active_booking_subquery)
        ).annotate(
             annotated_average_attendance=Case(
                When(
                    total_finished_bookings_for_rate__gt=0,
                    then=ExpressionWrapper(
                         100.0 * F('completed_bookings_count') / F('total_finished_bookings_for_rate'),
                         output_field=DecimalField(max_digits=5, decimal_places=2)
                    )
                ),
                default=Value(Decimal('0.00')),
                output_field=DecimalField(max_digits=5, decimal_places=2)
            )
        )


        # --- UPDATED PREFETCH ---
        can_view_notes = user.has_perm('quickstart.view_studentnote')
        if can_view_notes:
            # Define the queryset for notes, filtered by the CURRENT business
            notes_for_this_business_qs = StudentNote.objects.filter(
                business=business # Filter notes by the business context HERE
            ).select_related('author').order_by('-created_at')

            # Prefetch using the filtered queryset and store in a specific attribute
            queryset = queryset.prefetch_related(
                 Prefetch(
                     'business_notes', # The related_name on CustomUser model
                     queryset=notes_for_this_business_qs,
                     to_attr='notes_for_this_business' # Use a distinct attribute name
                 )
            )

        queryset = self.filter_queryset(queryset) # Apply search/ordering
        return queryset

    # --- UPDATE retrieve and list to NOT pass business context ---
    # The filtering is now done in the prefetch
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        # No need to pass business context anymore for notes filtering
        serializer = self.get_serializer(instance, context={'request': request})
        return Response(serializer.data)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        if page is not None:
             # No need to pass business context anymore for notes filtering
            serializer = self.get_serializer(page, many=True, context={'request': request})
            return self.get_paginated_response(serializer.data)

        # No need to pass business context anymore for notes filtering
        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)

    # --- Custom Actions ---
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanManageBusinessStudentNotes])
    def add_note(self, request, pk=None):
        """ Add a note to a specific student profile within the business context. """
        student_user = self.get_object() # Ensures student is associated with this business via get_queryset

        # Check specific 'add' permission
        if not request.user.has_perm('quickstart.add_studentnote'):
             raise PermissionDenied("You do not have permission to add notes.")

        # Get the business context (re-fetch for clarity or use from initial check)
        business = self.get_business_context()

        # Use the renamed note serializer
        serializer = BusinessStudentNoteSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            # Save the note, associating it correctly
            serializer.save(
                user=student_user,   # The student (already fetched and verified)
                business=business,   # The business context
                author=request.user  # The user writing the note
            )
            logger.info(f"Note added for student {student_user.email} (ID: {pk}) by {request.user.email} in business {business.businessName}")

            # Return the created note using the same serializer
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        else:
             logger.warning(f"Failed to add note for student {student_user.email} by {request.user.email}. Errors: {serializer.errors}")
             return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)