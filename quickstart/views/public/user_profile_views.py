from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ...serializers import MyProfileSerializer 

import logging
logger = logging.getLogger(__name__)

class MyProfileView(generics.RetrieveAPIView):
    """
    API endpoint for an authenticated user to retrieve their OWN profile details.
    Read-only view. Updates are handled by UserUpdateView.
    """
    serializer_class = MyProfileSerializer
    permission_classes = [IsAuthenticated] # Must be logged in

    def get_object(self):
        """
        Returns the currently authenticated user's profile.
        """
        # No need for complex filtering, just return the request user
        return self.request.user

    # Override retrieve for potential custom logic, though default works fine
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        # You could add extra data here if needed, e.g., global stats
        # response_data = serializer.data
        # response_data['global_booking_count'] = instance.bookings.count() # Example
        return Response(serializer.data)