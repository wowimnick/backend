from rest_framework import serializers
from ..models import Booking, CustomUser, StudentNote

from ..serializers import CustomUserDetailsSerializer

class StudentNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    author_avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = StudentNote
        fields = [
            'id', 'content', 'created_at', 'author',
            'author_name', 'author_avatar_url'
        ]
        read_only_fields = ['created_at', 'author', 'business']
    
    def get_author_name(self, obj):
        if obj.author:
            return f"{obj.author.first_name} {obj.author.last_name}"
        return None
    
    def get_author_avatar_url(self, obj):
        if obj.author and obj.author.avatar:
            return obj.author.avatar.url
        return None
    
class StudentProfileSerializer(serializers.ModelSerializer):
    notes = serializers.SerializerMethodField()
    active_classes = serializers.IntegerField(read_only=True)
    total_classes_taken = serializers.IntegerField(source='completed_bookings_count', read_only=True)
    average_attendance = serializers.DecimalField(
        max_digits=5, decimal_places=2, read_only=True
    )

    class Meta:
        model = CustomUser
        fields = [
            'userId', 'email', 'first_name', 'last_name',
            'birth_date', 'phone_number', 'country',
            'city', 'state', 'address', 'zipCode',
            'avatar', 'active_classes', 'total_classes_taken',
            'average_attendance', 'notes'
        ]
        read_only_fields = [
            'active_classes', 'total_classes_taken',
            'average_attendance', 'notes'
        ]

    def get_notes(self, obj):
        # Add logging
        print("Getting notes for user:", obj.userId)
        notes = getattr(obj, 'prefetched_notes', [])
        print("Found notes:", len(notes))
        
        # Try both potential sources of notes
        if not notes:
            notes = obj.business_notes.all()
            print("Fetched from business_notes:", len(notes))
            
        serialized = StudentNoteSerializer(notes, many=True).data
        print("Serialized notes:", serialized)
        return serialized