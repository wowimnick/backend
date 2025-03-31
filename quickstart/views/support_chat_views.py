from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from openai import OpenAI
from django.conf import settings
import logging
import re

from quickstart.models import ChatMessage, ChatSession
from ..serializers import ChatRequestSerializer, ChatMessageSerializer, ChatSessionSerializer, SupportTicketSerializer

logger = logging.getLogger(__name__)

class ChatMessageView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.OPENROUTER_API_KEY,
            # Add this to explicitly set the Authorization header
            default_headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "HTTP-Referer": settings.ALLOWED_HOSTS[0],
                "X-Title": "ClassEasily Chat",
            }
        )

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            # Log the API key being used (masked)
            logger.info(f"Using API key: ...{settings.OPENROUTER_API_KEY[-4:]}")
            
            # Check for session_id in the request
            session_id = request.data.get('session_id')
            is_new_session = False
            
            if session_id:
                try:
                    chat_session = ChatSession.objects.get(id=session_id, userId=request.user)
                    logger.info(f"Using existing chat session: {session_id}")
                except ChatSession.DoesNotExist:
                    logger.warning(f"Session {session_id} not found, creating new session")
                    chat_session = ChatSession.objects.create(userId=request.user)
                    is_new_session = True
            else:
                # Create a new session when no session_id is provided
                logger.info(f"Creating new chat session for user: {request.user.userId}")
                chat_session = ChatSession.objects.create(userId=request.user)
                is_new_session = True
            
            user_message = serializer.validated_data['messages'][-1]
            user_content = user_message['content']
            
            # Check if the message is just a single emoji shortcode
            emoji_pattern = r'^\s*:(\w+):\s*$'
            is_emoji_only = bool(re.match(emoji_pattern, user_content))
            
            # Store user message in database
            ChatMessage.objects.create(
                session=chat_session,
                content=user_content,
                is_user=True,
                sender_type='user'
            )
            
            # Create message array for the AI request
            all_messages = []
            
            # Only add system prompt for new sessions
            if is_new_session:
                all_messages.append({
                    "role": "system",
                    "content": settings.AI_SYSTEM_PROMPT
                })
            
            # For existing sessions, retrieve recent conversation history
            if not is_new_session:
                # Get the last N messages (adjust the number as needed)
                recent_messages = ChatMessage.objects.filter(
                    session=chat_session
                ).order_by('-created_at')[:10]
                
                # Add messages in chronological order
                for msg in reversed(recent_messages):
                    all_messages.append({
                        "role": "user" if msg.is_user else "assistant",
                        "content": msg.content
                    })
                
                # Also include system prompt to maintain context and instructions
                system_message = {
                    "role": "system",
                    "content": settings.AI_SYSTEM_PROMPT
                }
                
                # Insert system prompt at the beginning
                all_messages.insert(0, system_message)
            
            # Add current user message if not already included in history
            if user_message not in all_messages:
                all_messages.append(user_message)
            
            # Log the exact request being made
            request_data = {
                "extra_body": {},
                "model": "google/gemini-2.0-pro-exp-02-05:free",
                "messages": all_messages
            }
            logger.info(f"OpenRouter request data: {request_data}")
            
            completion = self.client.chat.completions.create(**request_data)
            
            logger.info(f"OpenRouter response: {completion}")
            
            # Get the AI response
            ai_content = completion.choices[0].message.content
            
            # Create the message
            ai_message = ChatMessage.objects.create(
                session=chat_session,
                content=ai_content,
                is_user=False,
                sender_type='ai'
            )
            
            # Create response data
            response_data = ChatMessageSerializer(ai_message).data
            
            # Add session ID to response
            response_data['session_id'] = chat_session.id
            
            return Response(response_data)
            
        except Exception as e:
            logger.error(f"Error in chat completion: {str(e)}")
            logger.error(f"Request headers: {getattr(e, 'request_headers', 'No headers available')}")
            logger.error(f"Response body: {getattr(e, 'response_body', 'No response body available')}")
            return Response(
                {'error': 'Failed to process chat message'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def get(self, request):
        """Get all chat sessions for the current user"""
        try:
            chat_sessions = ChatSession.objects.filter(userId=request.user).order_by('-updated_at')
            serializer = ChatSessionSerializer(chat_sessions, many=True)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error fetching chat sessions: {str(e)}")
            return Response(
                {'error': 'Failed to fetch chat sessions'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def notify_support_staff(self, ticket):
        """
        Send notification to support staff about new ticket
        This is a placeholder for the actual implementation
        """
        logger.info(f"Support ticket #{ticket.ticket_id} created by {ticket.user.email}: {ticket.subject}")
        # In a real implementation, you might send an email, Slack notification, etc.
        # You could implement this with Django signals or a task queue like Celery