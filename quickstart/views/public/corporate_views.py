import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from quickstart.serializers.public.corporate_serializers import (
    CorporateInquiryCreateSerializer,
)
from quickstart.tasks.corporate_tasks import queue_corporate_inquiry_emails

logger = logging.getLogger(__name__)


class CorporateInquiryCreateThrottle(AnonRateThrottle):
    rate = "12/hour"


class CorporateInquiryCreateView(APIView):
    """
    Public POST to submit a corporate / team-building inquiry.
    Persists the row and queues internal + confirmation via send_transactional_email_task
    (same Celery/Resend path as other transactional mail).
    """

    permission_classes = [AllowAny]
    throttle_classes = [CorporateInquiryCreateThrottle]

    def post(self, request):
        ser = CorporateInquiryCreateSerializer(
            data=request.data, context={"request": request}
        )
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

        inquiry = ser.save()
        try:
            queue_corporate_inquiry_emails(inquiry)
            logger.info("Corporate inquiry %s: transactional email tasks queued", inquiry.pk)
        except Exception as e:
            logger.exception("Failed to queue corporate inquiry emails: %s", e)

        return Response({"id": str(inquiry.id)}, status=status.HTTP_201_CREATED)
