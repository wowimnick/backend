from django.db.models import Count, Sum, Avg, Case, When, F, DecimalField, Q, Value, CharField, Exists, OuterRef
from django.db.models.functions import Coalesce, TruncDay, TruncMonth, TruncWeek, TruncQuarter, TruncYear
from django.utils import timezone
from datetime import timedelta
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
import csv
from django.http import HttpResponse
from io import StringIO

from ...models import BusinessInfo, ClassesMain, ClassOption, Reviews, Booking
from ...serializers import (
    BusinessInfoSerializer,
    BusinessStatsSerializer,
)
from ...utils.permissions import check_user_role, IsAdminUser


class BusinessAdminViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = BusinessInfoSerializer
    
    def get_queryset(self):
        """Return queryset with annotations for admin views"""
        queryset = BusinessInfo.objects.all()
        
        # Apply filters
        search = self.request.query_params.get('search', None)
        category = self.request.query_params.get('category', None)
        status = self.request.query_params.get('status', None)
        featured = self.request.query_params.get('featured', None)
        
        if search:
            queryset = queryset.filter(
                Q(businessName__icontains=search) | 
                Q(businessCity__icontains=search) | 
                Q(businessState__icontains=search) |
                Q(businessType__icontains=search)
            )
            
        if category:
            queryset = queryset.filter(classCategory=category)
            
        if status:
            if status == 'active':
                queryset = queryset.filter(isActive=True)
            elif status == 'inactive':
                queryset = queryset.filter(isActive=False)
            elif status == 'pending':
                queryset = queryset.filter(verificationStatus='pending')
            elif status == 'no_schedules':
                # Find businesses marked as active but with no active schedules
                queryset = queryset.filter(
                    isActive=True
                ).exclude(
                    classesmain__options__schedules__is_active=True,
                    classesmain__options__schedules__instances__status='scheduled',
                    classesmain__options__schedules__instances__date__gte=timezone.now().date()
                )
                
        if featured and featured.lower() == 'true':
            queryset = queryset.filter(featured=True)
            
        queryset = queryset.annotate(
            # Check if business has active schedules
            has_active_schedules=Exists(
                ClassOption.objects.filter(
                    classId__businessId=OuterRef('businessId'),
                    schedules__is_active=True,
                    schedules__instances__status='scheduled',
                    schedules__instances__date__gte=timezone.now().date()
                )
            ),
            
            # Count distinct classes to avoid duplicates
            classes_count=Count('classesmain', distinct=True),
            
            # Count bookings including completed ones
            bookings_count=Count(
                'classesmain__options__schedules__instances__bookings', 
                filter=Q(
                    classesmain__options__schedules__instances__bookings__status__in=['confirmed', 'completed']
                ),
                distinct=True
            ),
            
            # Sum revenue from both confirmed and completed bookings
            revenue=Coalesce(
                Sum(
                    Case(
                        When(
                            classesmain__options__schedules__instances__bookings__status__in=['confirmed', 'completed'],
                            then='classesmain__options__schedules__instances__bookings__amount_paid'
                        ),
                        default=0,
                        output_field=DecimalField(max_digits=10, decimal_places=2)
                    )
                ),
                0,  # Default to 0 if NULL
                output_field=DecimalField(max_digits=10, decimal_places=2)
            ),
            
            rating=Coalesce(
                Avg(
                    'classesmain__reviews__rating'
                ),
                Value(0.0)
            ),
            
            status=Case(
                When(isActive=True, has_active_schedules=True, then=Value('active')),
                When(isActive=True, has_active_schedules=False, then=Value('no_schedules')),
                When(verificationStatus='pending', then=Value('pending')),
                default=Value('inactive'),
                output_field=CharField(max_length=20)
            )
        )
        
        return queryset
        
    @action(detail=True, methods=['post'])
    def toggle_feature(self, request, pk=None):
        """Toggle featured status for a business"""
        business = self.get_object()
        featured = request.data.get('featured', not business.featured)
        
        business.featured = featured
        business.save()
        
        return Response({
            'businessId': business.businessId,
            'featured': business.featured
        })
        
    @action(detail=False, methods=['get'])
    def metrics(self, request):
        """Get admin dashboard metrics"""
        # Get current date for time-based calculations
        today = timezone.now().date()
        thirty_days_ago = today - timedelta(days=30)
        sixty_days_ago = today - timedelta(days=60)
        
        # Calculate metrics
        total_businesses = BusinessInfo.objects.count()
        active_businesses = BusinessInfo.objects.filter(isActive=True).count()
        
        # Calculate growth rate
        businesses_30d_ago = BusinessInfo.objects.filter(
            createdAt__lt=thirty_days_ago
        ).count()
        businesses_now = total_businesses
        
        if businesses_30d_ago > 0:
            total_business_growth = ((businesses_now - businesses_30d_ago) / businesses_30d_ago) * 100
        else:
            total_business_growth = 0
            
        # Featured businesses
        featured_businesses = BusinessInfo.objects.filter(featured=True).count()
        
        # New businesses in last 30 days
        new_businesses_30d = BusinessInfo.objects.filter(
            createdAt__gte=thirty_days_ago
        ).count()
        
        # Total revenue - FIXED calculation to include both confirmed and completed bookings
        total_revenue = Booking.objects.filter(
            status__in=['confirmed', 'completed']  # Include both statuses
        ).aggregate(
            total=Coalesce(Sum('amount_paid'), 0, output_field=DecimalField(max_digits=10, decimal_places=2))
        )['total'] or 0
        
        # Log the revenue calculation for debugging
        print(f"Total revenue calculation: {total_revenue}")
        
        # Category distribution
        category_distribution = []
        for category, color in [
            ('academic', '#3b82f6'),
            ('music', '#8b5cf6'),
            ('dance', '#ec4899'),
            ('fitness', '#10b981'),
            ('art', '#f97316'),
            ('technology', '#0ea5e9'),
            ('sports', '#ef4444')
        ]:
            count = BusinessInfo.objects.filter(classCategory=category).count()
            if count > 0:
                category_distribution.append({
                    'name': category.capitalize(),
                    'value': count,
                    'color': color
                })
        
        # Growth trend - default to monthly
        monthly_growth = []
        for i in range(6):
            month_end = today.replace(day=1) - timedelta(days=i*30)
            month_start = month_end.replace(day=1)
            month_name = month_start.strftime('%b')
            
            businesses = BusinessInfo.objects.filter(
                createdAt__lt=month_end
            ).count()
            
            revenue = Booking.objects.filter(
                status='confirmed',
                booking_date__lt=month_end,
                booking_date__gte=month_start
            ).aggregate(
                total=Sum('amount_paid')
            )['total'] or 0
            
            monthly_growth.append({
                'month': month_name,
                'businesses': businesses,
                'revenue': revenue
            })
            
        monthly_growth.reverse()
        
        # Location distribution
        location_distribution = []
        business_locations = BusinessInfo.objects.values('businessCity', 'businessState').annotate(
            count=Count('businessId')
        ).order_by('-count')[:12]
        
        for location in business_locations:
            city = location['businessCity']
            state = location['businessState']
            count = location['count']
            
            # Determine region
            region = self.get_region_for_province(state)
            
            location_distribution.append({
                'city': city,
                'state': state,
                'count': count,
                'region': region
            })
            
        # Top businesses
        top_businesses = self.get_queryset().order_by('-revenue')[:5]
        top_businesses_data = BusinessInfoSerializer(top_businesses, many=True).data
        
        # Return consolidated metrics
        return Response({
            'total_businesses': total_businesses,
            'active_businesses': active_businesses,
            'total_business_growth': total_business_growth,
            'featured_businesses': featured_businesses,
            'new_businesses_30d': new_businesses_30d,
            'total_revenue': float(total_revenue),
            'category_distribution': category_distribution,
            'growth_trend': monthly_growth,
            'location_distribution': location_distribution,
            'top_businesses': top_businesses_data
        })
        
    @action(detail=False, methods=['get'])
    def growth(self, request):
        """Get growth trends based on timeframe"""
        timeframe = request.query_params.get('timeframe', 'month')
        today = timezone.now().date()
        
        # Generate date ranges based on timeframe
        if timeframe == 'week':
            # Weekly data for past 12 weeks
            periods = 12
            period_length = 7  # days
        elif timeframe == 'month':
            # Monthly data for past 6 months
            periods = 6
            period_length = 30  # days
        elif timeframe == 'quarter':
            # Quarterly data for past 4 quarters
            periods = 4
            period_length = 90  # days
        else:  # year
            # Yearly data for past 3 years
            periods = 3
            period_length = 365  # days
        
        # Generate periods with real data
        result = []
        for i in range(periods):
            period_end = today - timedelta(days=i * period_length)
            period_start = period_end - timedelta(days=period_length)
            
            # Format period label based on timeframe
            if timeframe == 'week':
                period_label = period_start.strftime('%d %b')
            elif timeframe == 'month':
                period_label = period_start.strftime('%b')
            elif timeframe == 'quarter':
                quarter = ((period_start.month - 1) // 3) + 1
                period_label = f"Q{quarter} {period_start.year}"
            else:  # year
                period_label = str(period_start.year)
                
            # Count businesses created in this period
            businesses_in_period = BusinessInfo.objects.filter(
                createdAt__range=[period_start, period_end]
            ).count()
            
            # Calculate revenue in this period
            revenue_in_period = Booking.objects.filter(
                status__in=['confirmed', 'completed'],
                booking_date__range=[period_start, period_end]
            ).aggregate(
                total=Coalesce(Sum('amount_paid'), 0, output_field=DecimalField(max_digits=10, decimal_places=2))
            )['total'] or 0
            
            result.append({
                'month': period_label,  # keep 'month' key for frontend compatibility
                'businesses': businesses_in_period,
                'revenue': float(revenue_in_period)  # Convert Decimal to float for JSON serialization
            })
            
        # Reverse to get chronological order
        result.reverse()
        
        return Response(result)
        
    @action(detail=False, methods=['get'])
    def geographical(self, request):
        """Get geographical distribution of businesses"""
        view_type = request.query_params.get('view_type', 'province')
        data_type = request.query_params.get('data_type', 'count')
        
        if view_type == 'province':
            # Group by province (state)
            queryset = BusinessInfo.objects.values('businessState')
        else:
            # Group by city
            queryset = BusinessInfo.objects.values('businessCity', 'businessState')
            
        # Annotate with required metrics
        queryset = queryset.annotate(
            count=Count('businessId'),
            revenue=Sum(
                Case(
                    When(
                        classesmain__options__schedules__instances__bookings__status='confirmed',
                        then='classesmain__options__schedules__instances__bookings__amount_paid'
                    ),
                    default=0,
                    output_field=DecimalField(max_digits=10, decimal_places=2)
                )
            )
        )
        
        # Calculate growth if needed (only for province view)
        if data_type == 'growth' and view_type == 'province':
            today = timezone.now().date()
            year_ago = today - timedelta(days=365)
            
            # Current counts by province
            current_counts = {
                item['businessState']: item['count'] 
                for item in queryset
            }
            
            # Year ago counts by province
            year_ago_counts = {
                item['businessState']: item['count'] 
                for item in BusinessInfo.objects.filter(
                    createdAt__lt=year_ago
                ).values('businessState').annotate(
                    count=Count('businessId')
                )
            }
            
            # Calculate growth rates
            for item in queryset:
                province = item['businessState']
                current = current_counts.get(province, 0)
                past = year_ago_counts.get(province, 0)
                
                if past > 0:
                    growth = ((current - past) / past) * 100
                else:
                    growth = 0 if current == 0 else 100
                    
                item['growth'] = round(growth, 1)
                
        # Format response based on view type
        result = []
        for item in queryset:
            if view_type == 'province':
                province_code = self.get_province_code(item['businessState'])
                region = self.get_region_for_province(item['businessState'])
                result.append({
                    'province': province_code,
                    'count': item['count'],
                    'revenue': float(item['revenue']),
                    'growth': item.get('growth', 0),
                    'region': region
                })
            else:
                province_code = self.get_province_code(item['businessState'])
                region = self.get_region_for_province(item['businessState'])
                result.append({
                    'province': item['businessCity'],
                    'state_code': province_code,
                    'count': item['count'],
                    'revenue': float(item['revenue']),
                    'region': region
                })
                
        # Sort by count by default
        result.sort(key=lambda x: x['count'], reverse=True)
        
        # Limit to top entries for readability
        limit = 13 if view_type == 'province' else 10
        result = result[:limit]
            
        return Response(result)
        
    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export businesses data as CSV"""
        # Apply the same filters as in get_queryset
        queryset = self.get_queryset()
        
        # Create CSV response
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="businesses_export.csv"'
        
        # Create writer
        writer = csv.writer(response)
        
        # Write header
        writer.writerow([
            'Business ID', 'Business Name', 'Type', 'Category', 
            'Location', 'Status', 'Featured', 'Rating',
            'Reviews', 'Bookings', 'Classes', 'Revenue',
            'Created At'
        ])
        
        # Write data rows
        for business in queryset:
            writer.writerow([
                business.businessId,
                business.businessName,
                business.businessType,
                business.classCategory,
                f"{business.businessCity}, {business.businessState}",
                'Active' if business.isActive else 'Inactive',
                'Yes' if getattr(business, 'featured', False) else 'No',
                round(getattr(business, 'rating', 0), 1),
                business.totalReviews,
                getattr(business, 'bookings_count', 0),
                getattr(business, 'classes_count', 0),
                getattr(business, 'revenue', 0),
                business.createdAt.strftime('%Y-%m-%d %H:%M:%S')
            ])
            
        return response
        
    @action(detail=False, methods=['post'])
    def announcements(self, request):
        """Send announcements to businesses"""
        recipient_type = request.data.get('recipientType', 'all')
        title = request.data.get('title')
        message = request.data.get('message')
        urgency = request.data.get('urgency', 'normal')
        send_email = request.data.get('sendEmail', True)
        send_in_app = request.data.get('sendInApp', True)
        
        if not title or not message:
            return Response(
                {'error': 'Title and message are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Select recipient businesses based on type
        businesses = BusinessInfo.objects.all()
        
        if recipient_type == 'active':
            businesses = businesses.filter(isActive=True)
        elif recipient_type == 'featured':
            businesses = businesses.filter(featured=True)
        elif recipient_type == 'new':
            thirty_days_ago = timezone.now() - timedelta(days=30)
            businesses = businesses.filter(createdAt__gte=thirty_days_ago)
        elif recipient_type == 'verified':
            businesses = businesses.filter(verificationStatus='verified')
            
        # Get business owners' emails
        owner_emails = businesses.values_list('owner__email', flat=True)
        
        # In a real implementation, you would:
        # 1. Queue email sending using a task queue (Celery)
        # 2. Create notification records in the database for in-app notifications
        
        # For now, just return success with count
        return Response({
            'success': True,
            'recipient_count': len(owner_emails),
            'sent_emails': send_email,
            'sent_in_app': send_in_app
        })
        
    def get_province_code(self, province_name):
        """Map province full name to code"""
        province_map = {
            'Ontario': 'ON',
            'Quebec': 'QC',
            'British Columbia': 'BC',
            'Alberta': 'AB',
            'Manitoba': 'MB',
            'Saskatchewan': 'SK',
            'Nova Scotia': 'NS',
            'New Brunswick': 'NB',
            'Newfoundland and Labrador': 'NL',
            'Prince Edward Island': 'PE',
            'Northwest Territories': 'NT',
            'Yukon': 'YT',
            'Nunavut': 'NU'
        }
        
        # Try to match directly
        if province_name in province_map:
            return province_map[province_name]
            
        # Try to match by lowercase
        lowercase_map = {k.lower(): v for k, v in province_map.items()}
        if province_name.lower() in lowercase_map:
            return lowercase_map[province_name.lower()]
            
        # Check if it's already a code
        if province_name in province_map.values():
            return province_name
            
        # Default to province name if no match
        return province_name[:2].upper()
        
    def get_region_for_province(self, province):
            """Map province to region"""
            region_map = {
                'Ontario': 'Central',
                'Quebec': 'Eastern',
                'British Columbia': 'Western',
                'Alberta': 'Western',
                'Manitoba': 'Central',
                'Saskatchewan': 'Central',
                'Nova Scotia': 'Atlantic',
                'New Brunswick': 'Atlantic',
                'Newfoundland and Labrador': 'Atlantic',
                'Prince Edward Island': 'Atlantic',
                'Northwest Territories': 'Northern',
                'Yukon': 'Northern',
                'Nunavut': 'Northern',
                # Also add province codes
                'ON': 'Central',
                'QC': 'Eastern',
                'BC': 'Western',
                'AB': 'Western',
                'MB': 'Central',
                'SK': 'Central',
                'NS': 'Atlantic',
                'NB': 'Atlantic',
                'NL': 'Atlantic',
                'PE': 'Atlantic',
                'NT': 'Northern',
                'YT': 'Northern',
                'NU': 'Northern'
            }
            
            return region_map.get(province, 'Other')