from django.urls import path, include
from rest_framework.routers import DefaultRouter
from allauth.account.views import confirm_email
from django.views.generic import TemplateView
from .views import (
    # Auth
    CustomLoginView, CustomTokenObtainPairView, CustomTokenRefreshView,
    LogoutView, UserUpdateView, CustomRegisterView, UserRoleView,
    
    # Core Resources
    BusinessInfoViewSet, ClassList, ClassDetail, 
    SecureInstructorViewSet, SecureStudentViewSet,
    BookingViewSet, BookingStatusViewSet, ScheduleViewSet,
    RoleViewSet,
    
    # Additional Resources
    ClassReviews, ClassImageList, ClassImageDetail,
    ClassOptionList, ClassOptionDetail,
    
    # Utility Functions
    search_classes_by_location
)

# Initialize the router
router = DefaultRouter()

# Register viewsets
router.register(r'businesses', BusinessInfoViewSet, basename='business')
router.register(r'instructors', SecureInstructorViewSet, basename='instructor')
router.register(r'students', SecureStudentViewSet, basename='student')
router.register(r'bookings', BookingViewSet, basename='booking')
router.register(r'booking-statuses', BookingStatusViewSet, basename='booking-status')
router.register(r'schedules', ScheduleViewSet, basename='schedule')
router.register(r'roles', RoleViewSet, basename='role')

# URL Patterns
urlpatterns = [
    # Include router URLs
    path('', include(router.urls)),
    
    # Authentication URLs
    path('auth/', include([
        path('', include('dj_rest_auth.urls')),
        path('registration/', CustomRegisterView.as_view(), name='rest_register'),
        path('account-confirm-email/', 
             TemplateView.as_view(template_name="email_confirmation.html"),
             name='account_email_verification_sent'),
        path('account-confirm-email/<str:key>/', 
             confirm_email,
             name='account_confirm_email'),
    ])),
    
    # Token URLs
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    
    # User Management
    path('user/', include([
        path('role/', UserRoleView.as_view(), name='user-role'),
        path('update/', UserUpdateView.as_view(), name='user-update'),
    ])),
    
    # Classes
    path('classes/', include([
        path('', ClassList.as_view(), name='class-list'),
        path('search/', search_classes_by_location, name='class-search'),
        path('<int:pk>/', ClassDetail.as_view(), name='class-detail'),
        path('<int:pk>/reviews/', ClassReviews.as_view(), name='class-reviews'),
        path('<int:pk>/images/', ClassImageList.as_view(), name='class-images'),
        path('images/<int:pk>/', ClassImageDetail.as_view(), name='class-image-detail'),
        path('<int:pk>/options/', ClassOptionList.as_view(), name='class-options'),
        path('<int:pk>/options/<int:option_id>/', 
             ClassOptionDetail.as_view(),
             name='class-option-detail'),
    ])),
]