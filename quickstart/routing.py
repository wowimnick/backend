"""
WebSocket URL routing for conversation consumer.
"""

from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(
        r"api/ws/conversations/(?P<conversation_id>[0-9a-f-]+)/$",
        consumers.ConversationConsumer.as_asgi(),
    ),
]
