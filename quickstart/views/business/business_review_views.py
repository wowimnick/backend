from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from django.db.models import Q
import logging

from ...models import Reviews, BusinessInfo
from ...serializers.business.business_review_serializers import BusinessReviewSerializer

logger = logging.getLogger(__name__)

# --- Permissions ---
class CanManageOwnBusinessReviews(BasePermission):
    """
    Allows access if user has 'view_own_business_reviews' perm
    AND owns/manages the related business for the review.
    """
    message = "You do not have permission to manage reviews for this business."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        # Check base permission and business association
        return (
            user.has_perm('quickstart.view_own_business_reviews') and
            BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).exists()
        )

    def has_object_permission(self, request, view, obj):
        # obj is the Review instance
        user = request.user
        # Find the business context for the user (should exist if has_permission passed)
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            return False # Should not happen

        # Check if the review's class belongs to the user's business
        # Ensure obj.classId exists before accessing its businessId
        return obj.classId and obj.classId.businessId == business

# --- ViewSet ---
class BusinessReviewViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for Business Users to view and respond to reviews on their classes.
    """
    serializer_class = BusinessReviewSerializer
    permission_classes = [IsAuthenticated, CanManageOwnBusinessReviews]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'comment', 'user__first_name', 'user__last_name', 'user__email',
        'classId__title', 'rating', 'status'
    ]
    ordering_fields = ['createdAt', 'rating', 'status', 'classId__title']
    ordering = ['-createdAt']
    # Allow POST only for the 'respond' action
    http_method_names = ['get', 'post', 'head', 'options']

    def get_business_context(self):
        """Helper to get the business for the request user."""
        user = self.request.user
        try:
            # Use filter().first() for safety, although owner/manager link is usually unique
            business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
            if not business:
                 # This should be caught by permissions, but safeguard.
                 raise PermissionDenied("User not associated with any managed business.")
            return business
        except BusinessInfo.DoesNotExist:
             # Should not happen if permissions are correct
             raise PermissionDenied("Associated business not found.")


    def get_queryset(self):
        """ Filters queryset to reviews belonging to the user's associated business. """
        business = self.get_business_context()
        return Reviews.objects.filter(
            classId__businessId=business
        ).select_related(
            'userId', 'classId', 'booking' # Include relations needed by serializer
        ).distinct()

    @action(detail=True, methods=['post'], url_path='respond',
            permission_classes=[IsAuthenticated, CanManageOwnBusinessReviews]) # Add specific permission check if needed
    def respond(self, request, pk=None):
        """ Allows a business user to add/update their response to a review. """
        review = self.get_object() # Ensures user has permission for this specific review

        # Check specific permission for responding
        if not request.user.has_perm('quickstart.add_business_review_response'):
            raise PermissionDenied("You do not have permission to respond to reviews.")

        response_text = request.data.get('business_response', None)

        if response_text is None:
             raise DRFValidationError({'business_response': 'This field is required.'})
        if len(response_text.strip()) > 1000: # Example length limit
              raise DRFValidationError({'business_response': 'Response cannot exceed 1000 characters.'})

        # Update the response
        review.business_response = response_text.strip()
        review.save(update_fields=['business_response'])

        logger.info(f"Business user {request.user.email} responded to Review {pk}")

        serializer = self.get_serializer(review)
        return Response(serializer.data)

    # Block standard create/update/delete for reviews by business users
    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def partial_update(self, request, *args, **kwargs):
         # Allow update only if it targets 'business_response'? Too complex, use 'respond' action.
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
    def destroy(self, request, *args, **kwargs):
        # Businesses generally shouldn't delete reviews
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)