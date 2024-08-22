from django.urls import path, include, re_path
from .views import BusinessInfoDetail, BusinessInfoViewSet, ClassList, ClassDetail, ClassReviews, CustomLoginView, CustomTokenObtainPairView, UserUpdateView, get_google_maps_api_key, ClassImageList, ClassImageDetail, SubClassesViewSet, SubClassDetail, CustomRegisterView
from allauth.account.views import confirm_email
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.views.generic import TemplateView

urlpatterns = [
    path('auth/', include('dj_rest_auth.urls')),
    path('login/', CustomLoginView.as_view(), name='token_obtain_pair'),
    path('user/update/', UserUpdateView.as_view(), name='user-update'),
    path('token/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/registration/', CustomRegisterView.as_view(), name='rest_register'),
    re_path(r'^account-confirm-email/', TemplateView.as_view(template_name="email_confirmation.html"), name='account_email_verification_sent'),
    re_path(r'^account-confirm-email/(?P<key>[-:\w]+)/$', confirm_email, name='account_confirm_email'),
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