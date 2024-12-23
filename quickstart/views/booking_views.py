from rest_framework import viewsets, status
from rest_framework.response import Response
from datetime import datetime
from django.shortcuts import get_object_or_404
from django.db.models import Q

from ..models import (
    Booking, BookingStatus
)
from ..serializers import (
    BookingSerializer,
    BookingStatusSerializer
)
from .permissions import IsInstructor
from .permissions import check_user_role

class BookingStatusViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BookingStatus.objects.all()
    serializer_class = BookingStatusSerializer
    permission_classes = [IsInstructor]

class BookingViewSet(viewsets.ModelViewSet):
    queryset = Booking.objects.all()
    serializer_class = BookingSerializer
    permission_classes = [IsInstructor]

    def get_queryset(self):
        queryset = super().get_queryset().select_related(
            'student__user', 
            'instructor__user',
            'class_instance', 
            'subclass'
        )

        user = self.request.user
        if check_user_role(user, ['Admin', 'Super Admin']):
            pass
        elif check_user_role(user, ['Business Owner']):
            queryset = queryset.filter(class_instance__businessId__owner=user)
        elif check_user_role(user, ['Manager']):
            managed_businesses = user.managed_businesses.all()
            queryset = queryset.filter(class_instance__businessId__in=managed_businesses)
        elif check_user_role(user, ['Instructor']):
            queryset = queryset.filter(instructor__user=user)
        else:
            queryset = queryset.none()

        status = self.request.query_params.get('status', None)
        instructor = self.request.query_params.get('instructor', None)

        if status:
            if ',' in status:
                statuses = status.split(',')
                queryset = queryset.filter(status__name__in=statuses)
            else:
                queryset = queryset.filter(status__name=status)

        if instructor:
            queryset = queryset.filter(instructor_id=instructor)

        return queryset

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        
        # Convert class_date to proper format if it exists
        if 'class_date' in request.data:
            try:
                request.data['class_date'] = datetime.strptime(
                    request.data['class_date'],
                    '%Y-%m-%d %H:%M:%S'
                )
            except ValueError:
                return Response(
                    {"error": "Invalid date format. Use YYYY-MM-DD HH:MM:SS"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if getattr(instance, '_prefetched_objects_cache', None):
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)