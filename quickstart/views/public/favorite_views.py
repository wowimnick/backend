# Create a new file: views/user/favorite_views.py
import logging
from decimal import Decimal

from django.db.models import Avg, Count, Subquery, OuterRef, Value, DecimalField, IntegerField
from django.db.models.functions import Coalesce

from rest_framework import generics, permissions
from rest_framework.pagination import PageNumberPagination

# Adjust imports based on your project structure
from ...models import ClassesMain, Reviews, Schedule
from ...serializers import PublicClassSerializer # Reuse the public serializer

logger = logging.getLogger(__name__)

# --- Standard Pagination for Favorites ---
class FavoritesPagination(PageNumberPagination):
    page_size = 12 # Adjust as needed
    page_size_query_param = 'page_size'
    max_page_size = 48

# Consider refactoring these into a Manager method on ClassesMain later for DRYness
AVERAGE_RATING_SUBQUERY = Subquery(
    Reviews.objects.filter(classId=OuterRef('pk'), status='approved')
    .values('classId')
    .annotate(avg_rating=Avg('rating'))
    .values('avg_rating')[:1],
    output_field=DecimalField(max_digits=3, decimal_places=1)
)
REVIEW_COUNT_SUBQUERY = Subquery(
    Reviews.objects.filter(classId=OuterRef('pk'), status='approved')
    .values('classId')
    .annotate(count=Count('reviewId'))
    .values('count')[:1],
    output_field=IntegerField()
)
MIN_PRICE_SUBQUERY = Subquery(
     Schedule.objects.filter(
          option__classId=OuterRef('pk'),
     ).order_by('price').values('price')[:1],
     output_field=DecimalField(max_digits=10, decimal_places=2)
)

class MyFavoritesListView(generics.ListAPIView):
    """
    API endpoint to list classes favorited by the currently authenticated user.
    """
    serializer_class = PublicClassSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = FavoritesPagination

    def get_queryset(self):
        """
        Return a queryset of classes favorited by the current user,
        optimized and annotated similarly to the public listing.
        """
        user = self.request.user
        # Start with the user's favorited classes
        queryset = user.favorited.select_related(
            'businessId', 'category', 'subcategory'
        ).prefetch_related(
            'images',
            'options',
            'options__schedules'
        ).filter( # Ensure only active/verified classes/businesses appear even in favorites
            status='active',
            businessId__isActive=True,
            businessId__verificationStatus='verified'
        ).annotate(
            # Apply the same annotations as the public view for consistency
            average_rating=Coalesce(AVERAGE_RATING_SUBQUERY, Value(Decimal('0.0'))),
            review_count=Coalesce(REVIEW_COUNT_SUBQUERY, Value(0)),
            min_price=Coalesce(MIN_PRICE_SUBQUERY, None)
        ).order_by('-favorites__createdAt') # Order by when the user favorited it (most recent first)
        # Note: 'favorites__createdAt' assumes the through model `Favorites` has `createdAt`.
        # If using the default M2M, you might not have this field.
        # Alternatively, order by class creation date or title: .order_by('-createdAt')

        return queryset

    def get_serializer_context(self):
        """Pass request context to the serializer."""
        context = super().get_serializer_context()
        context.update({'request': self.request})
        return context