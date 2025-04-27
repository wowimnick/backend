from rest_framework import viewsets, filters
from rest_framework.permissions import AllowAny
import logging

# Adjust import paths based on your project structure
from ...models import BusinessInfo
from ...serializers.public.public_business_serializers import PublicBusinessInfoSerializer # Make sure this path is correct

logger = logging.getLogger(__name__)

class PublicBusinessInfoViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides READ-ONLY access to public Business Information.
    Filters to show only active and verified businesses.
    Supports searching and ordering.
    (URL Base: /api/businesses/)
    """
    serializer_class = PublicBusinessInfoSerializer
    permission_classes = [AllowAny] 

    # Add standard filtering/searching capabilities
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'businessName', 'businessDescription', 'businessCity',
        'businessState', 'classCategory', 'subcategories' # Add fields you want searchable
    ]
    ordering_fields = ['businessName', 'createdAt', 'featured'] # Add fields you want orderable
    ordering = ['-featured', 'businessName'] # Default ordering: featured first, then by name

    # --- REMOVE the class-level queryset attribute ---
    # queryset = BusinessInfo.objects.select_related('owner').filter(
    #     isActive=True,
    #     verificationStatus='verified'
    # ).order_by('-featured', 'businessName')

    # --- DEFINE get_queryset method ---
    def get_queryset(self):
        """
        Returns a queryset containing only active and verified businesses.
        Applies the default ordering.
        """
        logger.debug("Fetching public business info queryset (active & verified)")
        # Move the filtering logic here
        queryset = BusinessInfo.objects.select_related(
            'owner' # Keep selecting owner if needed by the PublicBusinessInfoSerializer
        ).filter(
            isActive=True,               # Ensure business is active
            verificationStatus='verified' # Ensure business is verified
        )

        # Apply default ordering specified in the class meta or ordering attribute
        # Note: OrderingFilter will handle request-based ordering (`?ordering=...`)
        # This ensures a default order if none is specified in the request.
        # The `ordering` attribute handles this automatically if defined.
        # If you need more complex default ordering logic, do it here.
        # queryset = queryset.order_by(*self.ordering) # DRF handles default ordering via 'ordering' attribute

        return queryset