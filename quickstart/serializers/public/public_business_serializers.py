from rest_framework import serializers
from ...models import BusinessInfo, ClassesMain, Reviews # Adjust import path as needed
from django.db.models import Avg

class PublicBusinessInfoSerializer(serializers.ModelSerializer):
    """
    Serializer for PUBLIC display of Business Information.
    Only includes fields safe for anyone to view.
    """
    average_rating = serializers.SerializerMethodField(read_only=True)
    totalReviews = serializers.IntegerField(read_only=True) # Use the optimized field from the model

    class Meta:
        model = BusinessInfo
        # Explicitly list ONLY public fields
        fields = [
            'businessId',
            'businessName',
            'businessType',
            'businessDescription',
            'businessImage', # URL is generated automatically by storage backend
            'openingTime',
            'closingTime',
            'cancellationPolicy', # Public policy info
            # Student contacts might be considered public for booking purposes
            'studentContactPhone',
            'studentContactEmail',
            # Public location info
            'businessCity',
            'businessState',
            'classCategory',
            # Public stats/metadata
            'totalReviews',
            'average_rating',
            'featured', 
            'createdAt', 
        ]
        read_only_fields = fields # All fields are read-only in this public serializer

    def get_average_rating(self, obj):
         # Consider filtering reviews by status='approved'
         # This calculation can be slow in lists, consider annotating in the view if possible
         avg = Reviews.objects.filter(businessId=obj, status='approved').aggregate(Avg('rating'))['rating__avg']
         return round(avg, 1) if avg else 0.0
