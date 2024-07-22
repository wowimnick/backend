from rest_framework import generics, viewsets
from .models import ClassesMain, Reviews, ClassImage
from .serializers import ClassesMainSerializer, ReviewSerializer, ClassImageSerializer
from django.http import JsonResponse
from rest_framework.response import Response
from django.conf import settings
from django.views.decorators.http import require_GET

class ClassList(generics.ListCreateAPIView):
    queryset = ClassesMain.objects.all()
    serializer_class = ClassesMainSerializer

    def perform_create(self, serializer):
        instance = serializer.save(
            classVideo=self.request.FILES.get('classVideo')
        )
        images = self.request.FILES.getlist('classImages')
        for image in images:
            ClassImage.objects.create(classId=instance, image=image)

class ClassDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = ClassesMain.objects.all()
    serializer_class = ClassesMainSerializer

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if 'classImages' in request.FILES:
            images = request.FILES.getlist('classImages')
            for image in images:
                ClassImage.objects.create(classId=instance, image=image)

        if getattr(instance, '_prefetched_objects_cache', None):
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)

class ClassImageList(generics.ListCreateAPIView):
    serializer_class = ClassImageSerializer

    def get_queryset(self):
        return ClassImage.objects.filter(classId=self.kwargs['pk'])

    def perform_create(self, serializer):
        class_instance = ClassesMain.objects.get(pk=self.kwargs['pk'])
        serializer.save(classId=class_instance)

class ClassImageDetail(generics.RetrieveDestroyAPIView):
    queryset = ClassImage.objects.all()
    serializer_class = ClassImageSerializer


class ClassReviews(generics.ListAPIView):
    serializer_class = ReviewSerializer

    def get_queryset(self):
        class_id = self.kwargs['pk']
        return Reviews.objects.filter(classId=class_id).select_related('userId')

@require_GET
def get_google_maps_api_key(request):
    return JsonResponse({'key': settings.GOOGLE_MAPS_API_KEY})