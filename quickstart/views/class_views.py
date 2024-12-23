from rest_framework import generics
from rest_framework.response import Response
from rest_framework.decorators import api_view
from django.shortcuts import get_object_or_404
from django.db.models import Exists, OuterRef
import logging

from ..models import (
    ClassesMain, ClassImage, Reviews, ClassOption
)
from ..serializers import (
    ClassesMainSerializer,
    ClassImageSerializer,
    ReviewSerializer,
    ClassOptionSerializer
)
from .utils import haversine_distance

logger = logging.getLogger(__name__)

class ClassList(generics.ListCreateAPIView):
    serializer_class = ClassesMainSerializer

    def get_queryset(self):
        queryset = ClassesMain.objects.all()
        
        private = self.request.query_params.get('private', None)
        group = self.request.query_params.get('group', None)

        if private == 'true':
            queryset = queryset.filter(
                Exists(ClassOption.objects.filter(classId=OuterRef('pk'), type='single'))
            )
        elif group == 'true':
            queryset = queryset.filter(
                Exists(ClassOption.objects.filter(classId=OuterRef('pk'), type='course'))
            )

        return queryset

    def perform_create(self, serializer):
        instance = serializer.save()
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

@api_view(['GET'])
def search_classes_by_location(request):
    """
    Search for classes based on coordinates within a radius.
    Required parameters: lat, lng
    Optional parameter: radius (in km, defaults to 10)
    """
    try:
        lat = request.GET.get('lat')
        lng = request.GET.get('lng')
        radius = float(request.GET.get('radius', 100))
        
        if not lat or not lng:
            return Response({
                "error": "Missing coordinates",
                "message": "Both latitude and longitude are required"
            }, status=400)
            
        search_lat = float(lat)
        search_lng = float(lng)

        classes = ClassesMain.objects.filter(isActive=True)
        
        classes_with_distance = []
        for class_obj in classes:
            try:
                if not class_obj.classCoordinates:
                    continue
                    
                class_lat, class_lng = map(
                    float, 
                    class_obj.classCoordinates.split(',')
                )
                
                distance = haversine_distance(
                    search_lat, search_lng,
                    class_lat, class_lng
                )
                
                if distance <= radius:
                    classes_with_distance.append((class_obj, distance))
                        
            except (ValueError, AttributeError) as e:
                logger.warning(f"Error processing class coordinates for class {class_obj.classId}: {str(e)}")
                continue

        classes_with_distance.sort(key=lambda x: x[1])
        
        serialized_classes = []
        for class_obj, distance in classes_with_distance:
            class_data = ClassesMainSerializer(class_obj).data
            class_data['distance'] = round(distance, 2)
            class_data['distance_text'] = (
                f"{round(distance, 1)}km away" if distance >= 1 
                else f"{round(distance * 1000)}m away"
            )
            serialized_classes.append(class_data)

        return Response({
            'results': serialized_classes,
            'total': len(serialized_classes),
            'search_coordinates': {
                'lat': search_lat,
                'lng': search_lng
            }
        })

    except ValueError as e:
        return Response({
            "error": "Invalid coordinates",
            "message": str(e)
        }, status=400)
    except Exception as e:
        logger.error(f"Unexpected error in search_classes_by_location: {str(e)}")
        return Response({
            "error": "Server error",
            "message": "An unexpected error occurred while processing your request"
        }, status=500)

class ClassOptionList(generics.ListCreateAPIView):
    serializer_class = ClassOptionSerializer

    def get_queryset(self):
        class_id = self.kwargs['pk']
        return ClassOption.objects.filter(classId=class_id)

    def perform_create(self, serializer):
        class_instance = ClassesMain.objects.get(pk=self.kwargs['pk'])
        serializer.save(classId=class_instance)
    
class ClassOptionDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = ClassOption.objects.all()
    serializer_class = ClassOptionSerializer

    def get_object(self):
        queryset = self.get_queryset()
        option_id = self.kwargs.get('option_id')
        obj = get_object_or_404(queryset, optionId=option_id)
        self.check_object_permissions(self.request, obj)
        return obj

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if 'image' in request.FILES:
            instance.image = request.FILES['image']
            instance.save()

        return Response(serializer.data)