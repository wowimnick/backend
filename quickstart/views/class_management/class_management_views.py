from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Min, Max, Avg, Count, F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from ...models import (
    ClassCategory, ClassesMain, ClassOption, Schedule, ScheduleInstance, 
    Booking, Reviews
)
from ...serializers.class_management.class_management_serializers import (
    AdminClassSerializer, 
    AdminClassDetailSerializer,
    AdminClassCreateSerializer,
    AdminClassCategorySerializer,
    AdminReviewSerializer,
    SubcategorySerializer
)
from ...utils.permissions import IsAdminUser
import logging 
logger = logging.getLogger(__name__)

class AdminClassViewSet(viewsets.ModelViewSet):
    """
    Admin-specific viewset for managing classes 
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminClassSerializer
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return AdminClassDetailSerializer
        elif self.action == 'create':
            return AdminClassCreateSerializer
        return AdminClassSerializer
    
    def get_queryset(self):
        try:
            """Get all classes with full annotations for admin use"""
            queryset = ClassesMain.objects.all()
            
            # Add business name annotation
            queryset = queryset.select_related('businessId').annotate(
                business_name=F('businessId__businessName')
            )
            
            # Calculate average rating for each class from reviews
            queryset = queryset.annotate(
                average_rating=Coalesce(Avg('reviews__rating'), 0.0),
                review_count=Count('reviews', distinct=True)
            )
            
            # Add price range annotations by checking all schedules across options
            queryset = queryset.annotate(
                min_price=Min('options__schedules__price'),
                max_price=Max('options__schedules__price')
            )
            
            # Add active classes count (based on schedules with future instances)
            current_date = timezone.now().date()
            queryset = queryset.annotate(
                active_schedules_count=Count(
                    'options__schedules__instances',
                    filter=Q(
                        options__schedules__instances__date__gte=current_date,
                        options__schedules__instances__status='scheduled'
                    ),
                    distinct=True
                )
            )
            
            # Filter based on request parameters
            category = self.request.query_params.get('category')
            if category and category != 'all':
                queryset = queryset.filter(category=category)
                
            status_filter = self.request.query_params.get('status')
            if status_filter:
                if status_filter == 'active':
                    queryset = queryset.filter(status='active')
                elif status_filter == 'inactive':
                    queryset = queryset.filter(status='inactive')
                elif status_filter == 'suspended':
                    queryset = queryset.filter(status='suspended')
                    
            featured = self.request.query_params.get('featured')
            if featured and featured.lower() == 'true':
                queryset = queryset.filter(businessId__featured=True)
                
            search = self.request.query_params.get('search')
            if search:
                queryset = queryset.filter(
                    Q(title__icontains=search) |
                    Q(businessId__businessName__icontains=search) |
                    Q(category__icontains=search) |
                    Q(location__icontains=search)
                )
                
            return queryset
        except Exception as e:
            logger.error(f"Error in AdminClassViewSet.get_queryset: {str(e)}", exc_info=True)
            raise
    
    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """Get analytics for classes"""
        # Count of all classes
        total_classes = ClassesMain.objects.count()
        
        # Count of active classes
        active_classes = ClassesMain.objects.filter(status='active').count()
        
        featured_classes = ClassesMain.objects.filter(businessId__featured=True).count()
        
        # Calculate average rating across all classes
        # We need to calculate this from all reviews, not retrieve it from a field
        avg_rating_result = Reviews.objects.aggregate(avg=Coalesce(Avg('rating'), 0.0))
        avg_rating = avg_rating_result['avg']
        
        # Category breakdown
        category_counts = dict(ClassesMain.objects.values_list('category').annotate(
            count=Count('classId')
        ).values_list('category', 'count'))
        
        # Status breakdown
        status_counts = {
            'active': ClassesMain.objects.filter(status='active').count(),
            'inactive': ClassesMain.objects.filter(status='inactive').count(),
            'suspended': ClassesMain.objects.filter(status='suspended').count()
        }
        
        # Most popular classes (by bookings)
        popular_classes = ClassesMain.objects.annotate(
            booking_count=Count('options__schedules__instances__bookings', distinct=True)
        ).order_by('-booking_count')[:5].values('classId', 'title', 'booking_count')
        
        # Highest rated classes - calculated from reviews, not stored field
        top_rated_classes = ClassesMain.objects.annotate(
            avg_rating=Avg('reviews__rating')
        ).exclude(avg_rating__isnull=True).order_by('-avg_rating')[:5].values(
            'classId', 'title', 'avg_rating'
        )
        
        return Response({
            'totalClasses': total_classes,
            'activeClasses': active_classes,
            'averageRating': round(avg_rating, 1),
            'categoryCounts': category_counts,
            'featuredClasses': featured_classes,
            'statusCounts': status_counts,
            'popularClasses': list(popular_classes),
            'topRatedClasses': list(top_rated_classes)
        })
    
    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export class data (simplified for example)"""
        # In a real implementation, you'd generate CSV/Excel file
        # For this demo, we'll just return JSON with the filtered data
        queryset = self.get_queryset()
        serializer = AdminClassSerializer(queryset, many=True)
        
        return Response({
            'exported': True,
            'count': queryset.count(),
            'data': serializer.data
        })

    @action(detail=True, methods=['post'])
    def update_class_status(self, request, pk=None):
        """Admin action to update class status (including suspension)"""
        class_instance = self.get_object()
        new_status = request.data.get('status')
        
        if new_status not in ['active', 'inactive', 'suspended']:
            return Response(
                {'error': 'Invalid status value. Must be one of: active, inactive, suspended'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        class_instance.status = new_status
        class_instance.save(update_fields=['status'])
        
        return Response({
            'status': class_instance.status,
            'classId': class_instance.classId
        })


class AdminCategoryViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing class categories
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminClassCategorySerializer
    
    def get_queryset(self):
        queryset = ClassCategory.objects.all()
        
        # Filter by search term if provided
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(name__icontains=search)
            
        return queryset
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get statistics for categories"""
        categories = ClassCategory.objects.annotate(
            total_classes=Count('classes'),
            active_classes=Count('classes', filter=Q(classes__status='active')),
            average_rating=Coalesce(Avg('classes__reviews__rating'), 0.0)
        )
        
        result = []
        for cat in categories:
            subcats = cat.subcategories.annotate(
                total=Count('classes')
            ).values('id', 'name', 'total')
            
            result.append({
                'id': cat.pk,
                'name': cat.name,
                'key': cat.key,
                'color': cat.color,
                'activeClasses': cat.active_classes,
                'totalClasses': cat.total_classes,
                'average_rating': round(cat.average_rating, 1),
                'subcategories': [
                    {
                        'id': sub['id'],
                        'name': sub['name'],
                        'count': sub['total']
                    }
                    for sub in subcats
                ]
            })
        
        return Response(result)
    
    @action(detail=True, methods=['post'])
    def subcategories(self, request, pk=None):
        """Add subcategory to a category"""
        category = self.get_object()
        
        serializer = SubcategorySerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(category=category)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class AdminReviewViewSet(viewsets.ModelViewSet):
    """
    Admin-specific viewset for managing reviews
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = AdminReviewSerializer
    
    def get_queryset(self):
        queryset = Reviews.objects.all().select_related(
            'userId', 'classId', 'businessId'
        )
        
        # Filter based on request parameters
        status_filter = self.request.query_params.get('status')
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)
            
        reported = self.request.query_params.get('reported')
        if reported and reported.lower() == 'true':
            queryset = queryset.filter(reported=True)
            
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(comment__icontains=search) |
                Q(userId__username__icontains=search) |
                Q(classId__title__icontains=search)
            )
            
        return queryset
    
    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        """Update review status"""
        review = self.get_object()
        status = request.data.get('status')
        
        if not status:
            return Response(
                {'error': 'Status is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        review.status = status
        review.save()
        
        return Response({'success': True})