from decimal import Decimal
from rest_framework import serializers
from ...models import Booking, BusinessInfo, CustomUser, StudentNote

# Renamed from StudentNoteSerializer
class BusinessStudentNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    author_avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = StudentNote
        fields = [
            'id', 'content', 'created_at', 'author',
            'author_name', 'author_avatar_url',
        ]
        read_only_fields = ['id', 'created_at', 'author', 'author_name', 'author_avatar_url']

    def get_author_name(self, obj):
        if obj.author:
            parts = [name for name in [obj.author.first_name, obj.author.last_name] if name]
            return " ".join(parts) if parts else obj.author.email
        return "Unknown Author"

    def get_author_avatar_url(self, obj):
        if obj.author and obj.author.avatar and hasattr(obj.author.avatar, 'url'):
            try:
                return obj.author.avatar.url
            except ValueError:
                 return None
        return None

class BusinessStudentProfileSerializer(serializers.ModelSerializer):
    average_attendance = serializers.DecimalField(
        source='annotated_average_attendance',
        max_digits=5, decimal_places=2, read_only=True, default=Decimal('0.00')
    )
    active_classes = serializers.IntegerField(source='active_bookings_count', read_only=True, default=0)
    total_classes_taken = serializers.IntegerField(source='completed_bookings_count', read_only=True, default=0)
    is_active = serializers.BooleanField(source='is_active_student', read_only=True)
    avatar_url = serializers.SerializerMethodField()

    # --- UPDATE notes field definition ---
    notes = BusinessStudentNoteSerializer(
        source='notes_for_this_business', 
        many=True,
        read_only=True
    )

    class Meta:
        model = CustomUser
        fields = [
            'userId', 'email', 'first_name', 'last_name',
            'phone_number',
            'avatar_url',
            'active_classes',
            'total_classes_taken',
            'average_attendance',
            'notes', # Field name remains 'notes' in the JSON output
            'is_active',
            'createdAt'
        ]
        # All fields are effectively read-only in this serializer context
        read_only_fields = fields

    def get_avatar_url(self, obj):
        if obj.avatar and hasattr(obj.avatar, 'url'):
             try:
                 return obj.avatar.url
             except ValueError:
                 return None
        return None