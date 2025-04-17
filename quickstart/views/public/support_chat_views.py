from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from openai import OpenAI # Ensure openai package is installed
from django.conf import settings
import logging
import re # For regex matching

from quickstart.models import ChatMessage, ChatSession, SupportTicket # Added SupportTicket
from ...serializers import ChatRequestSerializer, ChatMessageSerializer, ChatSessionSerializer, SupportTicketSerializer # Import necessary serializers

logger = logging.getLogger(__name__)

class ChatMessageView(views.APIView):
    permission_classes = [IsAuthenticated]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize OpenAI client using environment variables
        # Ensure OPENROUTER_API_KEY and other settings are correctly configured
        try:
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
            logger.info("OpenAI client initialized successfully for OpenRouter.")
        except AttributeError as e:
             logger.error(f"Missing required setting for OpenRouter: {e}. Chat functionality may be disabled.")
             self.client = None # Disable client if settings are missing
        except Exception as e:
             logger.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
             self.client = None


    def post(self, request):
        if not self.client:
             # Return an error if the client couldn't be initialized
             return Response(
                  {'error': 'Chat service is currently unavailable due to configuration issues.'},
                  status=status.HTTP_503_SERVICE_UNAVAILABLE
             )


        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        session_id = serializer.validated_data.get('session_id')
        user_message_content = serializer.validated_data['messages'][-1]['content']

        try:
            # --- Get or Create Chat Session ---
            if session_id:
                try:
                    # Ensure the session belongs to the current user
                    chat_session = ChatSession.objects.get(id=session_id, userId=user)
                    is_new_session = False
                    logger.info(f"Using existing chat session: {session_id} for user {user.email}")
                except ChatSession.DoesNotExist:
                    logger.warning(f"Session {session_id} not found for user {user.email}, creating new session.")
                    # Fallback to creating a new session if ID is invalid or doesn't belong to user
                    chat_session = ChatSession.objects.create(userId=user)
                    is_new_session = True
            else:
                chat_session = ChatSession.objects.create(userId=user)
                is_new_session = True
                logger.info(f"Created new chat session {chat_session.id} for user {user.email}")

            # --- Store User Message ---
            ChatMessage.objects.create(
                session=chat_session,
                content=user_message_content,
                is_user=True,
                sender_type='user'
            )

            # --- Prepare Messages for AI ---
            messages_for_ai = []
            system_prompt = getattr(settings, 'AI_SYSTEM_PROMPT', "You are a helpful assistant.") # Default prompt

            if is_new_session:
                 messages_for_ai.append({"role": "system", "content": system_prompt})
            else:
                 # Add system prompt first for context
                 messages_for_ai.append({"role": "system", "content": system_prompt})
                 # Retrieve recent messages (e.g., last 10)
                 recent_db_messages = ChatMessage.objects.filter(
                      session=chat_session
                 ).order_by('-created_at')[:10] # Fetch recent messages
                 # Add them in chronological order (oldest first)
                 for msg in reversed(recent_db_messages):
                      messages_for_ai.append({
                           "role": "user" if msg.is_user else "assistant",
                           "content": msg.content
                      })

            # Add the current user message (it's already saved, but needed for the AI's context)
            messages_for_ai.append({"role": "user", "content": user_message_content})


            # --- Call AI Service ---
            ai_model = getattr(settings, 'OPENROUTER_CHAT_MODEL', "google/gemini-flash-1.5") # Default model
            logger.info(f"Sending {len(messages_for_ai)} messages to model {ai_model}")

            completion = self.client.chat.completions.create(
                model=ai_model,
                messages=messages_for_ai
                # Add other parameters like temperature, max_tokens if needed from settings
                # temperature=getattr(settings, 'AI_TEMPERATURE', 0.7),
                # max_tokens=getattr(settings, 'AI_MAX_TOKENS', 150),
            )

            # --- Process and Store AI Response ---
            ai_response_content = completion.choices[0].message.content
            logger.info(f"Received AI response for session {chat_session.id}")

            ai_message = ChatMessage.objects.create(
                session=chat_session,
                content=ai_response_content,
                is_user=False,
                sender_type='ai'
            )

            # --- Prepare API Response ---
            response_serializer = ChatMessageSerializer(ai_message)
            response_data = response_serializer.data
            response_data['session_id'] = chat_session.id # Ensure session ID is returned

            return Response(response_data, status=status.HTTP_200_OK) # Use 200 OK for successful chat turn


        except Exception as e:
            # Log detailed error, especially if it's from the OpenAI client
            if hasattr(e, 'status_code'): # OpenAI API error
                logger.error(f"OpenAI API Error ({e.status_code}): {e.response}", exc_info=False)
                error_detail = f"AI service error: {e.status_code}"
                response_status = status.HTTP_502_BAD_GATEWAY # Indicate upstream failure
            else: # Other exceptions
                logger.error(f"Error processing chat message for user {user.email}, session {session_id}: {str(e)}", exc_info=True)
                error_detail = 'Failed to process chat message due to an internal error.'
                response_status = status.HTTP_500_INTERNAL_SERVER_ERROR

            return Response(
                {'error': error_detail},
                status=response_status
            )

    def get(self, request):
        """Get all chat sessions (metadata, not messages) for the current user"""
        user = request.user
        try:
            # Order sessions by most recently updated
            chat_sessions = ChatSession.objects.filter(userId=user).order_by('-updated_at')
            # Use a serializer that perhaps doesn't include all messages for list view efficiency
            # Assuming ChatSessionSerializer includes basic info + maybe last message snippet
            serializer = ChatSessionSerializer(chat_sessions, many=True, context={'request': request})
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error fetching chat sessions for user {user.email}: {str(e)}", exc_info=True)
            return Response(
                {'error': 'Failed to fetch chat sessions.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    # notify_support_staff is likely better handled by signals or async tasks
    # triggered upon ticket creation/update, rather than being a method here.
    # def notify_support_staff(self, ticket): ...