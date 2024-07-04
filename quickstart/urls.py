# urls.py
from django.urls import path
from .views import ClassList, ClassDetail
from .views import get_google_maps_api_key

urlpatterns = [
    path('classes/', ClassList.as_view(), name='class-list'),
    path('classes/<int:pk>/', ClassDetail.as_view(), name='class-detail'),
    path('google-maps-key/', get_google_maps_api_key, name='google_maps_api_key'),
]
