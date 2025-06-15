from rest_framework import serializers
from decimal import Decimal # Ensure Decimal is imported

from ....models import (
    ClassCategory, ClassSubcategory, ClassesMain, ClassImage, ClassOption, Reviews,
    Schedule, ScheduleInstance, BusinessInfo
)
from ....serializers.public.public_review_serializers import UserReviewSerializer # Keep this import

# --- AdminScheduleInstanceSerializer ---
class AdminScheduleInstanceSerializer(serializers.ModelSerializer):
    booking_count = serializers.IntegerField(source='current_bookings', read_only=True)
    available_spots = serializers.IntegerField(read_only=True)

    class Meta:
        model = ScheduleInstance
        fields = [
            'id', 'date', 'time', 'duration', 'price',
            'max_participants', 'status', 'booking_count',
            'available_spots', 
            'cancellation_reason',
            'created_at', 'updated_at'
        ]

# --- AdminScheduleSerializer ---
class AdminScheduleSerializer(serializers.ModelSerializer):
    instances = AdminScheduleInstanceSerializer(many=True, read_only=True)

    class Meta:
        model = Schedule
        fields = [
            'id', 'day', 'time', 'duration', 'price',
            'maxParticipants', 'start_date', 'end_date',
            'date', 'allow_late_enrollment',
            'instances'
        ]

# --- AdminClassOptionSerializer ---
class AdminClassOptionSerializer(serializers.ModelSerializer):
    schedules = AdminScheduleSerializer(many=True, read_only=True)
    price_range = serializers.SerializerMethodField()
    total_students = serializers.IntegerField(read_only=True, default=0)
    parent_class_title = serializers.CharField(source='classId.title', read_only=True)

    class Meta:
        model = ClassOption
        fields = [
            'optionId', 'parent_class_title',
            'booking_type', 'level',
            'price_type', 'equipment', 'tags',
            'cancellationPolicy', 'schedules',
            'price_range',
            'total_students', 'createdAt', 'updatedAt'
        ]
        read_only_fields = [
            'optionId', 'parent_class_title', 'price_range', 
            'total_students', 'createdAt', 'updatedAt'
        ]

    def get_price_range(self, obj):
        # In a real scenario, this should likely filter on active schedules
        # but for now, we'll keep it simple as per the original.
        all_schedules = obj.schedules.all()
        if not all_schedules:
            return None

        prices = [schedule.price for schedule in all_schedules if schedule.price is not None]
        if not prices:
            return None

        min_price = min(prices)
        max_price = max(prices)

        if min_price == max_price:
            return {'min': min_price, 'max': min_price, 'single_price': True}

        return {'min': min_price, 'max': max_price, 'single_price': False}

# --- AdminClassImageSerializer ---
class AdminClassImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassImage
        fields = ['imageId', 'image', 'createdAt']

# --- AdminBusinessSerializer (Simplified) ---
class AdminBusinessSerializer(serializers.ModelSerializer):
    businessImage = serializers.ImageField(read_only=True, use_url=True)

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'businessType',
            'businessImage',
            'businessCity', 'businessState',
            'verificationStatus', 'isActive', 'featured'
        ]

class AdminClassSerializer(serializers.ModelSerializer):
    images = AdminClassImageSerializer(many=True, read_only=True)
    business = AdminBusinessSerializer(source='businessId', read_only=True)
    businessId = serializers.IntegerField(source='businessId.pk', read_only=True)
    business_name = serializers.CharField(read_only=True) # Will be populated by the annotation in the view
    business_featured = serializers.BooleanField(read_only=True, default=False)
    average_rating = serializers.FloatField(read_only=True, default=0.0)
    review_count = serializers.IntegerField(read_only=True, default=0)
    active_schedules_count = serializers.IntegerField(read_only=True, default=0)
    min_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, allow_null=True)
    max_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, allow_null=True)
    category = serializers.SerializerMethodField()
    subcategory = serializers.SerializerMethodField()
    price_range = serializers.SerializerMethodField()
    platform_revenue = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, default=Decimal('0.00'))

    class Meta:
        model = ClassesMain
        fields = [
            'classId', 'title', 'description',
            'category', 'subcategory', 'location',
            'min_price', 'max_price', # Included here now
            'price_range', 'status',
            'active_schedules_count', 'business_featured',
            'images', 'business', 'businessId', 'business_name', # MODIFIED: Added business_name
            'average_rating', 'review_count',
            'platform_revenue',
            'createdAt', 'updatedAt'
        ]
        read_only_fields = fields

    def get_price_range(self, obj):
        min_price = getattr(obj, 'min_price', None)
        max_price = getattr(obj, 'max_price', None)

        # Fallback logic if annotations are None (requires prefetch of options__schedules)
        if min_price is None and max_price is None:
             prices = []
             # This fallback is inefficient and should be avoided by ensuring annotations are always present
             if hasattr(obj, 'options'):
                 for option in obj.options.all(): # Assumes prefetch_related('options__schedules')
                     for schedule in option.schedules.all():
                          if schedule.price is not None:
                              prices.append(schedule.price)
                 if not prices: return None
                 min_price = min(prices)
                 max_price = max(prices)
        elif min_price is None: # Handle only max exists
             min_price = max_price
        elif max_price is None: # Handle only min exists
             max_price = min_price

        if min_price is None: return None # Still no price found

        min_price_dec = Decimal(str(min_price))
        max_price_dec = Decimal(str(max_price))

        if min_price_dec == max_price_dec:
            return {'min': min_price_dec, 'max': max_price_dec, 'single_price': True}
        else:
            return {'min': min_price_dec, 'max': max_price_dec, 'single_price': False}

    def get_category(self, obj):
        if obj.category: return {'id': obj.category.id, 'name': obj.category.name, 'key': obj.category.key, 'color': obj.category.color}; return None
    def get_subcategory(self, obj):
        if obj.subcategory: return {'id': obj.subcategory.id, 'name': obj.subcategory.name, 'key': obj.subcategory.key}; return None
        
# --- AdminReviewSerializer ---
class AdminReviewSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    className = serializers.CharField(source='classId.title', read_only=True)
    businessName = serializers.CharField(source='businessId.businessName', read_only=True)

    class Meta:
        model = Reviews
        fields = [
            'reviewId', 'userId', 'user', 'classId', 'className',
            'businessId', 'businessName', 'rating', 'comment',
            'status', 'reported', 'report_reason', 'business_response',
            'createdAt'
        ]
        read_only_fields = ['reviewId', 'userId', 'user', 'classId', 'className', 'businessId', 'businessName', 'createdAt', 'rating', 'comment'] # Define fields admin cannot edit directly

    def get_user(self, obj):
        # Ensure user object exists and has necessary fields
        if obj.userId:
             # Use related name or standard user fields
             full_name = f"{obj.userId.first_name} {obj.userId.last_name}".strip()
             return {
                 'id': obj.userId.userId, # Assuming pk is userId
                 'name': full_name or obj.userId.email, # Fallback to email
                 'avatar_url': obj.userId.get_avatar_url() if hasattr(obj.userId, 'get_avatar_url') else None
             }
        return None
    
# --- AdminClassDetailSerializer ---
class AdminClassDetailSerializer(AdminClassSerializer):
    """
    Detailed serializer for single class view in admin panel.
    Inherits fields from AdminClassSerializer and adds options/reviews.
    """
    options = AdminClassOptionSerializer(many=True, read_only=True)
    reviews = AdminReviewSerializer(many=True, read_only=True)

    class Meta(AdminClassSerializer.Meta):
        fields = list(AdminClassSerializer.Meta.fields) + [
            'options',
            'reviews', # Keep 'reviews' here
            'studentContactEmail', 'studentContactPhone',
            'features',
        ]
        # Read-only fields are inherited, add new ones if needed
        read_only_fields = fields # Keep detail view read-only for now
# --- AdminClassCreateSerializer ---
class AdminClassCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating/updating classes through admin panel
    """
    class Meta:
        model = ClassesMain
        fields = [
            'title', 'description', 'category', 'subcategory',
            'location', 'coordinates', 'status',
            'studentContactEmail', 'studentContactPhone'
            # Add 'businessId' if admin needs to specify it during creation
        ]
        # Add write_only=True for fields like businessId if needed

# --- SubcategorySerializer ---
class SubcategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassSubcategory
        fields = ['id', 'name', 'key', 'description']

# --- AdminClassCategorySerializer ---
class AdminClassCategorySerializer(serializers.ModelSerializer):
    subcategories = SubcategorySerializer(many=True, read_only=True)
    activeClasses = serializers.IntegerField(read_only=True, default=0, source='active_classes')

    class Meta:
        model = ClassCategory
        fields = [
            'id', 'name', 'key', 'color',
            'created_at', 'updated_at',
            'subcategories',
            'activeClasses', # This will now be populated by the annotation
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'activeClasses']