from rest_framework import generics
from .models import ClassesMain, Reviews
from .serializers import ClassesMainSerializer, ReviewSerializer
from django.http import JsonResponse
from django.conf import settings
from django.views.decorators.http import require_GET

class ClassList(generics.ListCreateAPIView):
    queryset = ClassesMain.objects.all()
    serializer_class = ClassesMainSerializer

class ClassDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = ClassesMain.objects.all()
    serializer_class = ClassesMainSerializer

class ClassReviews(generics.ListAPIView):
    serializer_class = ReviewSerializer

    def get_queryset(self):
        class_id = self.kwargs['pk']
        return Reviews.objects.filter(classId=class_id).select_related('userId')

@require_GET
def get_google_maps_api_key(request):
    return JsonResponse({'key': settings.GOOGLE_MAPS_API_KEY})