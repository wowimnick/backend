# views.py
from rest_framework import generics
from .models import ClassesMain
from .serializers import ClassesMainSerializer
from django.http import JsonResponse
from django.conf import settings
from django.views.decorators.http import require_GET

class ClassList(generics.ListCreateAPIView):
    queryset = ClassesMain.objects.all()
    serializer_class = ClassesMainSerializer

class ClassDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = ClassesMain.objects.all()
    serializer_class = ClassesMainSerializer

@require_GET
def get_google_maps_api_key(request):
    return JsonResponse({'key': settings.GOOGLE_MAPS_API_KEY})
