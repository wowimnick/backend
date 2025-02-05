from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied

from django.shortcuts import get_object_or_404

from ..models import (
    Instructor, BusinessInfo, Education, 
    Certification, Skill, InstructorNote
)
from ..serializers import (
    InstructorSerializer, EducationSerializer,
    CertificationSerializer, SkillSerializer,
    InstructorNoteSerializer
)
from ..utils.permissions import (
    BaseUserDataPermission, IsManager, IsInstructor, check_user_role
)

import logging
logger = logging.getLogger(__name__)

class InstructorViewSet(viewsets.ModelViewSet):
    serializer_class = InstructorSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = Instructor.objects.select_related('user', 'business').prefetch_related(
            'education', 'certifications', 'skills', 
            'notes', 'notes__author'
        )

        # Handle search
        search = self.request.query_params.get('search', '')
        if search:
            queryset = queryset.filter(
                Q(user__first_name__icontains=search) |
                Q(user__last_name__icontains=search) |
                Q(user__email__icontains=search) |
                Q(specialization__icontains=search)
            )

        # Handle filters
        specialization = self.request.query_params.get('specialization')
        if specialization and specialization != 'all':
            queryset = queryset.filter(specialization=specialization)

        employment_type = self.request.query_params.get('employment_type')
        if employment_type and employment_type != 'all':
            queryset = queryset.filter(employment_type=employment_type)

        # Filter based on user role
        user = self.request.user
        if check_user_role(user, ['Admin', 'Super Admin']):
            return queryset
        elif check_user_role(user, ['Business Owner']):
            return queryset.filter(business__owner=user)
        elif check_user_role(user, ['Manager']):
            return queryset.filter(business__managers=user)
        elif check_user_role(user, ['Instructor']):
            return queryset.filter(user=user)
        return queryset.none()

    @action(detail=True, methods=['post'])
    def add_education(self, request, pk=None):
        instructor = self.get_object()
        serializer = EducationSerializer(data=request.data)
        
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_certification(self, request, pk=None):
        instructor = self.get_object()
        serializer = CertificationSerializer(data=request.data)
        
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_skill(self, request, pk=None):
        instructor = self.get_object()
        serializer = SkillSerializer(data=request.data)
        
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        instructor = self.get_object()
        serializer = InstructorNoteSerializer(data=request.data)
        
        if serializer.is_valid():
            serializer.save(
                instructor=instructor,
                author=request.user
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def perform_create(self, serializer):
        # Verify business access if specified
        business_id = self.request.data.get('business')
        if business_id:
            business = get_object_or_404(BusinessInfo, id=business_id)
            if not (check_user_role(self.request.user, ['Admin']) or 
                   business.owner == self.request.user or 
                   business.managers.filter(id=self.request.user.id).exists()):
                raise PermissionDenied("You don't have permission to add instructors to this business")
        serializer.save()

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsManager]
        else:
            permission_classes = [IsAuthenticated]
        return [permission() for permission in permission_classes]