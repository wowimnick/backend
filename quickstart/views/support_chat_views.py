from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from openai import OpenAI
from django.conf import settings
import logging
import re

from quickstart.utils.support_ticket_utils import process_for_ticket_creation
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
            
            chat_session = ChatSession.objects.get_or_create(
                userId_id=request.user.userId
            )[0]
            
            user_message = serializer.validated_data['messages'][-1]
            user_content = user_message['content']
            
            # Check if the message is just a single emoji shortcode
            emoji_pattern = r'^\s*:(\w+):\s*$'
            is_emoji_only = bool(re.match(emoji_pattern, user_content))
            
            # Handle single emoji case (store the shortcode itself)
            ChatMessage.objects.create(
                session=chat_session,
                content=user_content,
                is_user=True
            )
            
            # Create message array with system prompt including chat termination instructions
            all_messages = [
                {"role": "system", "content": settings.AI_SYSTEM_PROMPT + settings.TICKET_CREATION_PROMPT + settings.CHAT_TERMINATION_INSTRUCTIONS}
            ]
            
            # Add all user and assistant messages
            for msg in serializer.validated_data['messages']:
                all_messages.append(msg)
            
            # Log the exact request being made
            request_data = {
                "extra_body": {},
                "model": "google/gemini-2.0-flash-thinking-exp:free",
                "messages": all_messages
            }
            logger.info(f"OpenRouter request data: {request_data}")
            
            completion = self.client.chat.completions.create(**request_data)
            
            logger.info(f"OpenRouter response: {completion}")
            
            # Get the AI response
            ai_content = completion.choices[0].message.content
            
            # Check if AI wants to terminate the chat
            terminate_match = re.search(r'<TERMINATE_CHAT>\s*(.*?)\s*</TERMINATE_CHAT>', ai_content, re.DOTALL)

            if terminate_match:
                # Extract termination reason
                termination_reason = terminate_match.group(1).strip()
                
                # Clean the response by removing the termination tags
                # This ensures we don't show empty messages if that's all there was
                cleaned_content = re.sub(r'<TERMINATE_CHAT>.*?</TERMINATE_CHAT>', '', ai_content, flags=re.DOTALL).strip()
                
                # Create a response that includes both the termination flag and cleaned content
                response_data = {
                    'text': cleaned_content,  # This might be empty if the entire message was just the termination tag
                    'isUser': False,
                    'timestamp': ChatMessage.objects.create(
                        session=chat_session,
                        content=cleaned_content if cleaned_content else "Chat terminated.",
                        is_user=False
                    ).created_at.isoformat(),
                    'terminate_chat': True,
                    'termination_reason': termination_reason
                }
                
                # Log the termination
                logger.info(f"Chat terminated by AI. Reason: {termination_reason}")
                
                return Response(response_data)
            
            # Process for ticket creation if not terminated
            processed_content, ticket = process_for_ticket_creation(
                ai_content,
                request.user,
                chat_session
            )
            
            # Create the message
            ai_message = ChatMessage.objects.create(
                session=chat_session,
                content=processed_content,
                is_user=False
            )
            
            # Create response data
            response_data = ChatMessageSerializer(ai_message).data
            
            # Add ticket info if a ticket was created and log it
            if ticket:
                logger.info(f"Adding ticket {ticket.ticket_id} to response")
                response_data['ticket'] = {
                    'id': ticket.ticket_id,
                    'subject': ticket.subject,
                    'category': ticket.category,
                    'priority': ticket.priority
                }
                
                # Log the full response data
                logger.info(f"Response with ticket: {response_data}")
            
            return Response(response_data)
            
        except Exception as e:
            logger.error(f"Error in chat completion: {str(e)}")
            logger.error(f"Request headers: {getattr(e, 'request_headers', 'No headers available')}")
            logger.error(f"Response body: {getattr(e, 'response_body', 'No response body available')}")
            return Response(
                {'error': 'Failed to process chat message'},
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

    def get(self, request):
        try:
            chat_session = ChatSession.objects.get(userId_id=request.user.userId)
            serializer = ChatSessionSerializer(chat_session)
            return Response(serializer.data)
        except ChatSession.DoesNotExist:
            return Response([])