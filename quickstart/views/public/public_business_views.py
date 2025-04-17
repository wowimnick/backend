from rest_framework import viewsets
from rest_framework.permissions import AllowAny
import logging

# Relative imports from the parent 'quickstart' app directory assumed
from ...models import BusinessInfo
from ...serializers import PublicBusinessInfoSerializer

logger = logging.getLogger(__name__)

# --- Public Views ---

class PublicBusinessInfoViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides READ-ONLY access to public Business Information.
    Filters to show only active and verified businesses.
    (URL Base: /api/businesses/)
    """
    # --- Use the specific public serializer ---
    serializer_class = PublicBusinessInfoSerializer
    # -----------------------------------------
    permission_classes = [AllowAny]

    queryset = BusinessInfo.objects.select_related('owner').filter(
        isActive=True,
        verificationStatus='verified'
    ).order_by('-featured', 'businessName')