from django.urls import path
from .views import BusinessInfoDetail, BusinessInfoViewSet, ClassList, ClassDetail, ClassReviews, get_google_maps_api_key, ClassImageList, ClassImageDetail, SubClassesViewSet, SubClassDetail

urlpatterns = [
    path('classes/', ClassList.as_view(), name='class-list'),
    path('classes/<int:pk>/', ClassDetail.as_view(), name='class-detail'),
    path('classes/<int:pk>/reviews/', ClassReviews.as_view(), name='class-reviews'),
    path('classes/<int:pk>/subclasses/', SubClassesViewSet.as_view(), name='class-subclasses'),
    path('classes/<int:pk>/subclasses/<int:subclass_id>/', SubClassDetail.as_view(), name='class-subclass-detail'),
    path('classes/<int:pk>/images/', ClassImageList.as_view(), name='class-images'),
    path('classes/images/<int:pk>/', ClassImageDetail.as_view(), name='class-image-detail'),
    path('businesses/', BusinessInfoViewSet.as_view(), name='business-list'),
    path('businesses/<int:pk>/', BusinessInfoDetail.as_view(), name='business-detail'),
    path('google-maps-key/', get_google_maps_api_key, name='google_maps_api_key'),
]