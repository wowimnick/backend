"""
Public (unauthenticated) guest messaging: submit a message from class page, and guest inbox via token.
"""

from django.utils import timezone
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import NotFound, ValidationError

from quickstart.models import (
    Conversation,
    ConversationMessage,
    BusinessInfo,
    Contact,
    ClassesMain,
)
from quickstart.serializers.public.public_conversation_serializers import (
    GuestMessageCreateSerializer,
    ConversationDetailSerializer,
    ConversationMessageSerializer,
    ConversationMessageCreateSerializer,
)
from quickstart.utils.guest_inbox_token import create_guest_inbox_token, parse_guest_inbox_token
from quickstart.utils.conversation_emails import notify_business_new_message

import logging

logger = logging.getLogger(__name__)


class GuestMessageCreateView(APIView):
    """
    POST: Unauthenticated guest sends first message to a business.
    Creates or finds Contact, creates or finds Conversation (booker_contact), creates message,
    notifies business, and sends guest an email with a link to view/reply (guest inbox token).
    """

    permission_classes = [AllowAny]

    def post(self, request):
        ser = GuestMessageCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        business = BusinessInfo.objects.filter(businessId=data["business_id"]).first()
        if not business:
            raise NotFound("Business not found.")

        contact = Contact.objects.filter(
            business=business, email__iexact=data["email"]
        ).first()
        if not contact:
            contact = Contact.objects.create(
                business=business,
                first_name=(data["first_name"] or "").strip(),
                last_name=(data.get("last_name") or "").strip(),
                email=data["email"],
                source="class_page_contact",
            )
        else:
            contact.first_name = (data["first_name"] or "").strip()
            contact.last_name = (data.get("last_name") or "").strip()
            contact.email = data["email"]
            contact.save(update_fields=["first_name", "last_name", "email"])

        conv = Conversation.objects.filter(
            business=business, booker_contact=contact
        ).first()
        if not conv:
            conv = Conversation(
                business=business,
                booker_user=None,
                booker_contact=contact,
                booking=None,
            )
            conv.full_clean()
            conv.save()

        msg = ConversationMessage.objects.create(
            conversation=conv,
            sender_type=ConversationMessage.SENDER_BOOKER,
            sender_user=None,
            sender_contact=contact,
            text=data["message"],
        )
        conv.last_message_at = timezone.now()
        conv.save(update_fields=["last_message_at"])

        notify_business_new_message(conv, msg)

        # Send email to guest with magic link to inbox
        from django.conf import settings
        from quickstart.utils.email_utils import send_templated_email

        token = create_guest_inbox_token(str(conv.id), str(contact.id))
        guest_inbox_url = f"{settings.FRONTEND_BASE_URL}/?guest_inbox_token={token}"
        class_title = None
        if data.get("class_id"):
            cls = ClassesMain.objects.filter(classId=data["class_id"]).first()
            if cls:
                class_title = getattr(cls, "title", None)
        send_templated_email(
            recipient_list=[contact.email],
            template_name="emails/guest_message_received_confirmation.html",
            context={
                "guest_name": f"{contact.first_name} {contact.last_name}".strip() or contact.email,
                "business_name": business.businessName,
                "guest_inbox_url": guest_inbox_url,
                "class_title": class_title,
            },
            subject=f"Your message was sent – {business.businessName}",
        )

        return Response(
            {
                "conversation_id": str(conv.id),
                "message": "Message sent. Check your email for a link to view and reply to this conversation.",
                "guest_inbox_url": guest_inbox_url,
            },
            status=status.HTTP_201_CREATED,
        )


class GuestInboxView(APIView):
    """
    GET: Return conversation + messages for a valid guest inbox token (query param: token).
    """

    permission_classes = [AllowAny]

    def get(self, request):
        token = request.query_params.get("token")
        if not token:
            raise ValidationError({"token": "Missing token."})
        parsed = parse_guest_inbox_token(token)
        if not parsed:
            raise ValidationError({"token": "Invalid or expired link. Request a new link from the business."})
        conversation_id, contact_id = parsed

        conv = (
            Conversation.objects.filter(id=conversation_id, booker_contact_id=contact_id)
            .select_related("business", "booker_contact")
            .prefetch_related("messages__sender_user", "messages__sender_contact")
            .first()
        )
        if not conv:
            raise NotFound("Conversation not found.")
        serializer = ConversationDetailSerializer(conv)
        return Response(serializer.data)


class GuestInboxSendView(APIView):
    """
    POST: Guest (via token) sends a message. Body: token, text.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token") or request.query_params.get("token")
        if not token:
            raise ValidationError({"token": "Missing token."})
        parsed = parse_guest_inbox_token(token)
        if not parsed:
            raise ValidationError({"token": "Invalid or expired link. Request a new link from the business."})
        conversation_id, contact_id = parsed

        conv = Conversation.objects.filter(
            id=conversation_id, booker_contact_id=contact_id
        ).select_related("booker_contact").first()
        if not conv:
            raise NotFound("Conversation not found.")

        ser = ConversationMessageCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        text = ser.validated_data["text"]

        contact = conv.booker_contact
        msg = ConversationMessage.objects.create(
            conversation=conv,
            sender_type=ConversationMessage.SENDER_BOOKER,
            sender_user=None,
            sender_contact=contact,
            text=text,
        )
        conv.last_message_at = timezone.now()
        conv.save(update_fields=["last_message_at"])
        notify_business_new_message(conv, msg)
        from quickstart.utils.conversation_ws_broadcast import broadcast_new_message
        broadcast_new_message(msg)

        return Response(
            ConversationMessageSerializer(msg).data,
            status=status.HTTP_201_CREATED,
        )


class GuestInboxMarkReadView(APIView):
    """
    POST: Guest (via token) marks the conversation as read. Body: token (or query param).
    """

    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token") or request.query_params.get("token")
        if not token:
            raise ValidationError({"token": "Missing token."})
        parsed = parse_guest_inbox_token(token)
        if not parsed:
            raise ValidationError({"token": "Invalid or expired link. Request a new link from the business."})
        conversation_id, contact_id = parsed

        conv = Conversation.objects.filter(
            id=conversation_id, booker_contact_id=contact_id
        ).first()
        if not conv:
            raise NotFound("Conversation not found.")

        conv.last_read_by_booker_at = timezone.now()
        conv.save(update_fields=["last_read_by_booker_at"])
        return Response({"last_read_by_booker_at": conv.last_read_by_booker_at})
