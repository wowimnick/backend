from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
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
from .permissions import (
    BaseUserDataPermission, IsManager, IsInstructor
)

import logging
logger = logging.getLogger(__name__)

class SecureInstructorViewSet(viewsets.ModelViewSet):
    queryset = Instructor.objects.all()
    serializer_class = InstructorSerializer
    permission_classes = [BaseUserDataPermission]

    def get_queryset(self):
        user = self.request.user
        if user.has_role('Admin') or user.has_role('Super Admin'):
            return Instructor.objects.all()
        elif user.has_role('Business Owner'):
            return Instructor.objects.filter(business__owner=user)
        elif user.has_role('Manager'):
            return Instructor.objects.filter(business__in=user.managed_businesses.all())
        elif user.has_role('Instructor'):
            return Instructor.objects.filter(user=user)
        return Instructor.objects.none()

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsManager]
        else:
            permission_classes = [IsInstructor]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        # Verify business access if specified
        business_id = self.request.data.get('business')
        if business_id:
            business = get_object_or_404(BusinessInfo, id=business_id)
            if not (self.request.user.has_role('Admin') or 
                   business.owner == self.request.user or 
                   business in self.request.user.managed_businesses.all()):
                raise PermissionDenied("You don't have permission to add instructors to this business")
        serializer.save()

    @action(detail=True, methods=['post'])
    def add_education(self, request, pk=None):
        instructor = self.get_object()
        if not (request.user.has_role('Admin') or instructor.user == request.user):
            raise PermissionDenied("You don't have permission to add education to this instructor")
        serializer = EducationSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_certification(self, request, pk=None):
        instructor = self.get_object()
        if not (request.user.has_role('Admin') or instructor.user == request.user):
            raise PermissionDenied("You don't have permission to add certifications to this instructor")
        serializer = CertificationSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_skill(self, request, pk=None):
        instructor = self.get_object()
        if not (request.user.has_role('Admin') or instructor.user == request.user):
            raise PermissionDenied("You don't have permission to add skills to this instructor")
        serializer = SkillSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def add_note(self, request, pk=None):
        instructor = self.get_object()
        if not (request.user.has_role('Admin') or request.user.has_role('Manager')):
            raise PermissionDenied("You don't have permission to add notes")
        serializer = InstructorNoteSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(instructor=instructor, author=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)