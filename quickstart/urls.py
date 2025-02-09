from django.urls import path, include
from rest_framework.routers import DefaultRouter
from allauth.account.views import confirm_email
from django.views.generic import TemplateView

from quickstart.payments.views import CreatePaymentIntentView, ProcessBookingWebhook

from .views import (
    # Existing views
    CustomLoginView, CustomTokenObtainPairView, CustomTokenRefreshView,
    LogoutView, UserUpdateView, CustomRegisterView, UserRoleView,
    BusinessInfoViewSet, InstructorViewSet, BookingViewSet,
    RoleViewSet, BusinessViewSet, register_business, 
    ScheduleViewSet, ClassReviews, ClassImageList,
    ClassImageDetail, ClassOptionDetail, StudentProfileViewSet,
    RevenueAnalyticsView, ClassViewSet, 
    
    # New views for instance-based system
    ScheduleInstanceViewSet,
    ScheduleBreakViewSet
)

# Initialize the router
router = DefaultRouter()

# Register existing viewsets
router.register(r'businesses', BusinessInfoViewSet, basename='business')
router.register(r'business-stats', BusinessViewSet, basename='business-stats')
router.register(r'instructors', InstructorViewSet, basename='instructor')
router.register(r'bookings', BookingViewSet, basename='booking')
router.register(r'roles', RoleViewSet, basename='role')
router.register(r'students', StudentProfileViewSet, basename='student')
router.register(r'classes', ClassViewSet, basename='classes')
# Register schedule-related viewsets
router.register(r'schedules', ScheduleViewSet, basename='schedule')
router.register(r'schedule-instances', ScheduleInstanceViewSet, basename='schedule-instance')
router.register(r'schedule-breaks', ScheduleBreakViewSet, basename='schedule-break')

# URL Patterns
urlpatterns = [
    # Include router URLs
    path('', include(router.urls)),
    path('silk/', include('silk.urls', namespace='silk')),
    
    # Existing auth URLs
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
    
    # Existing token URLs
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    
    # Existing user management
    path('user/', include([
        path('role/', UserRoleView.as_view(), name='user-role'),
        path('update/', UserUpdateView.as_view(), name='user-update'),
    ])),

    path('business/register/', register_business, name='business-register'),
    
    # Classes and related URLs
    path('classes/', include([
        path('images/<int:pk>/', ClassImageDetail.as_view(), name='class-image-detail'),
        path('<int:pk>/reviews/', ClassReviews.as_view(), name='class-reviews'),
        path('<int:pk>/images/', ClassImageList.as_view(), name='class-images'),
        path('<int:pk>/options/', 
            ClassOptionDetail.as_view(),
            name='class-option-create'),
        path('<int:pk>/options/<int:option_id>/', 
            ClassOptionDetail.as_view(),
            name='class-option-detail'),
    ])),

    # New schedule instance-specific URLs
    path('schedule-instances/<int:pk>/', include([
        path('mark-attendance/', 
             ScheduleInstanceViewSet.as_view({'post': 'mark_attendance'}),
             name='mark-attendance'),
        path('cancel/',
             ScheduleInstanceViewSet.as_view({'post': 'cancel'}),
             name='cancel-instance'),
    ])),

    path('revenue/analytics/', RevenueAnalyticsView.as_view(), name='revenue-analytics'),

    path('payments/webhook/', ProcessBookingWebhook.as_view(), name='payment-webhook'),
    path('payments/create-payment-intent/', CreatePaymentIntentView.as_view(), name='create-payment-intent'),

    path('my_bookings/', BookingViewSet.as_view({'get': 'my_bookings'}), name='my-bookings'),
    path('<int:pk>/student_cancel/', BookingViewSet.as_view({'post': 'student_cancel'}), name='student-cancel'),
    path('<int:pk>/student_reschedule/', BookingViewSet.as_view({'post': 'student_reschedule'}), name='student-reschedule'),
]