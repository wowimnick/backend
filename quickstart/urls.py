from django.urls import path
from .views import ClassList, ClassDetail, ClassReviews, get_google_maps_api_key, ClassImageList, ClassImageDetail

urlpatterns = [
    path('classes/', ClassList.as_view(), name='class-list'),
    path('classes/<int:pk>/', ClassDetail.as_view(), name='class-detail'),
    path('classes/<int:pk>/images/', ClassImageList.as_view(), name='class-image-list'),
    path('classes/images/<int:pk>/', ClassImageDetail.as_view(), name='class-image-detail'),
    path('classes/<int:pk>/reviews/', ClassReviews.as_view(), name='class-reviews'),
    path('google-maps-key/', get_google_maps_api_key, name='google_maps_api_key'),
]