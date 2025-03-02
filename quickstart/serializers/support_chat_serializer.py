import re
from rest_framework import serializers

from ..models import ChatMessage, ChatSession, SupportTicket

class ChatMessageSerializer(serializers.ModelSerializer):
    isUser = serializers.BooleanField(source='is_user')
    timestamp = serializers.DateTimeField(source='created_at')
    text = serializers.CharField(source='content')

    class Meta:
        model = ChatMessage 
        fields = ['id', 'text', 'isUser', 'timestamp']

class ChatSessionSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)
    
    class Meta:
        model = ChatSession
        fields = ['id', 'userId', 'created_at', 'updated_at', 'messages']
        read_only_fields = ['id', 'created_at', 'updated_at']

class ChatRequestSerializer(serializers.Serializer):
    messages = serializers.ListField(
        child=serializers.DictField(
            child=serializers.CharField(),
            allow_empty=False
        ),
        min_length=1
    )

    def validate_messages(self, value):
        """
        Validate that each message has the required fields and format
        """
        valid_roles = ['user', 'assistant']
        
        for message in value:
            if 'role' not in message or 'content' not in message:
                raise serializers.ValidationError(
                    "Each message must have 'role' and 'content' fields"
                )
            if message['role'] not in valid_roles:
                raise serializers.ValidationError(
                    f"Message role must be one of {valid_roles}"
                )
            
            # Check if it's a single emoji message (may appear empty after stripping HTML)
            emoji_pattern = r'^\s*:(\w+):\s*$'
            is_emoji_shortcode = bool(re.match(emoji_pattern, message['content'].strip()))
            
            # Only validate for non-empty content if it's not an emoji shortcode
            if not is_emoji_shortcode and not message['content'].strip():
                raise serializers.ValidationError(
                    "Message content cannot be empty"
                )
        
        return value
    
class SupportTicketSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    
    class Meta:
        model = SupportTicket
        fields = [
            'ticket_id', 'category', 'subject', 'description', 
            'status', 'priority', 'created_at', 'username', 'user_email'
        ]
        read_only_fields = ['ticket_id', 'created_at']

class TicketCreationResponseSerializer(serializers.Serializer):
    """Serializer for including ticket information in chat responses"""
    message = ChatMessageSerializer()
    ticket = SupportTicketSerializer()