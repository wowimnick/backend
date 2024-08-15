from django.shortcuts import get_object_or_404
from rest_framework import generics, viewsets
from .models import BusinessInfo, ClassesMain, Reviews, ClassImage, SubClasses
from .serializers import BusinessInfoSerializer, ClassesMainSerializer, ReviewSerializer, ClassImageSerializer, SubClassesSerializer
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

class SubClassesViewSet(generics.ListCreateAPIView):
    serializer_class = SubClassesSerializer

    def get_queryset(self):
        class_id = self.kwargs['pk']
        return SubClasses.objects.filter(classId=class_id)

    def perform_create(self, serializer):
        class_instance = ClassesMain.objects.get(pk=self.kwargs['pk'])
        serializer.save(classId=class_instance)
    
class SubClassDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = SubClasses.objects.all()
    serializer_class = SubClassesSerializer

    def get_object(self):
        queryset = self.get_queryset()
        subclass_id = self.kwargs.get('subclass_id')
        obj = get_object_or_404(queryset, subclassId=subclass_id)
        self.check_object_permissions(self.request, obj)
        return obj

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if 'subclassImage' in request.FILES:
            instance.subclassImage = request.FILES['subclassImage']
            instance.save()

        return Response(serializer.data)
    
class BusinessInfoViewSet(generics.ListCreateAPIView):
    queryset = BusinessInfo.objects.all()
    serializer_class = BusinessInfoSerializer

    def perform_create(self, serializer):
        serializer.save(userId=self.request.user)

class BusinessInfoDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = BusinessInfo.objects.all()
    serializer_class = BusinessInfoSerializer

    def get_object(self):
        queryset = self.get_queryset()
        obj = get_object_or_404(queryset, businessId=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data
        # Add user info to the response
        user_data = {
            'first_name': instance.userId.first_name,
            'last_name': instance.userId.last_name,
            'email': instance.userId.email,
        }
        data['user'] = user_data
        
        # Add related classes
        related_classes = ClassesMain.objects.filter(businessId=instance)
        class_data = ClassesMainSerializer(related_classes, many=True).data
        data['classes'] = class_data
        
        return Response(data)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if 'businessImage' in request.FILES:
            instance.businessImage = request.FILES['businessImage']
            instance.save()

        return Response(serializer.data)


@require_GET
def get_google_maps_api_key(request):
    return JsonResponse({'key': settings.GOOGLE_MAPS_API_KEY})