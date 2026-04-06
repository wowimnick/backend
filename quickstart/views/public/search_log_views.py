from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from quickstart.models import SearchLog


class SearchLogCreateThrottle(AnonRateThrottle):
    rate = "120/hour"


class SearchLogCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SearchLog
        fields = [
            "query",
            "location",
            "province",
            "latitude",
            "longitude",
            "session_id",
            "results_count",
        ]


class SearchLogCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [SearchLogCreateThrottle]

    def post(self, request):
        ser = SearchLogCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        user = request.user if request.user.is_authenticated else None
        log = SearchLog.objects.create(
            **ser.validated_data,
            user=user,
        )
        return Response({"id": log.id}, status=status.HTTP_201_CREATED)
