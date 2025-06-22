from rest_framework import serializers
from ...models import BusinessInfo, ClassesMain, Reviews
from django.db.models import Avg

class BusinessContactDetailSerializer(serializers.ModelSerializer):
    """
    A secure serializer that ONLY exposes contact details for users
    who are authorized to see them (i.e., after booking).
    """
    class Meta:
        model = BusinessInfo
        fields = [
            'studentContactPhone',
            'studentContactEmail',
            'website', 
        ]
        read_only_fields = fields

class PublicBusinessInfoSerializer(serializers.ModelSerializer):
    """
    Serializer for PUBLIC display of Business Information.
    Only includes fields safe for anyone to view.
    Conditionally hides contact information based on the business's privacy settings.
    """
    average_rating = serializers.DecimalField(max_digits=3, decimal_places=1, read_only=True)
    totalReviews = serializers.IntegerField(source='total_reviews_count', read_only=True)
    
    # Create a new read-only field that gets the name from the related category object.
    classCategoryName = serializers.CharField(source='classCategory.name', read_only=True, allow_null=True)

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId',
            'businessName',
            'businessType',
            'businessDescription',
            'businessImage',
            'website',              
            'social_media_links',   
            'business_timezone',    
            'openingTime',
            'closingTime',
            'studentContactPhone', 
            'studentContactEmail',  
            'businessCity',
            'businessState',
            'classCategoryName', 
            'totalReviews',
            'average_rating',
            'featured',
            'contact_privacy',     
            'founding_year',
            'createdAt',
        ]
        read_only_fields = fields 

    def to_representation(self, instance):
        """
        Modify the serialized data before it's returned.
        This is where we'll remove sensitive contact info based on privacy settings.
        """
        # Get the default serialized representation
        representation = super().to_representation(instance)

        # Check the privacy setting on the model instance
        if instance.contact_privacy != 'public':
            representation.pop('studentContactPhone', None)
            representation.pop('studentContactEmail', None)
            representation.pop('website', None) 

        return representation

    def get_average_rating(self, obj):
         avg = Reviews.objects.filter(businessId=obj, status='approved').aggregate(Avg('rating'))['rating__avg']
         return round(avg, 1) if avg else 0.0