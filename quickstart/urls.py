from django.urls import path, include
from rest_framework.routers import DefaultRouter
from allauth.account.views import confirm_email
from django.views.generic import TemplateView

from quickstart.monitoring.consumers import MetricsConsumer
from quickstart.payments.views import CreatePaymentIntentView, ProcessBookingWebhook

from .views import (
    # Existing views
    CustomLoginView, CustomTokenObtainPairView, CustomTokenRefreshView,
    LogoutView, UserUpdateView, CustomRegisterView, UserRoleView,
    BusinessInfoViewSet, BookingViewSet,
    RoleViewSet, BusinessViewSet, register_business, 
    ScheduleViewSet, ClassReviews, ClassImageList,
    ClassImageDetail, ClassOptionDetail, StudentProfileViewSet,
    RevenueAnalyticsView, ClassViewSet, ReviewSubmission, ChatMessageView,
    ScheduleInstanceViewSet,
    ScheduleBreakViewSet
)

from .views.user_management.user_admin_views import UserAdminViewSet
from .views.user_management.role_views import RoleManagementViewSet
from .views.user_management.verification_views import VerificationRequestViewSet
from .views.user_management.audit_views import AuditLogViewSet

# Initialize the router
router = DefaultRouter()

# Register existing viewsets
router.register(r'businesses', BusinessInfoViewSet, basename='business')
router.register(r'business-stats', BusinessViewSet, basename='business-stats')
router.register(r'bookings', BookingViewSet, basename='booking')
router.register(r'roles', RoleViewSet, basename='role')
router.register(r'students', StudentProfileViewSet, basename='student')
router.register(r'classes', ClassViewSet, basename='classes')
router.register(r'schedules', ScheduleViewSet, basename='schedule')
router.register(r'schedule-instances', ScheduleInstanceViewSet, basename='schedule-instance')
router.register(r'schedule-breaks', ScheduleBreakViewSet, basename='schedule-break')

user_management_router = DefaultRouter()
user_management_router.register(r'admin/users', UserAdminViewSet, basename='admin-users')
user_management_router.register(r'admin/roles', RoleManagementViewSet, basename='admin-roles')
user_management_router.register(r'admin/verification', VerificationRequestViewSet, basename='admin-verification')
user_management_router.register(r'admin/audit-logs', AuditLogViewSet, basename='admin-audit-logs')


# URL Patterns
urlpatterns = [
    path('', include(router.urls)),
    path('silk/', include('silk.urls', namespace='silk')),
    path('ws/system_metrics/', MetricsConsumer.as_asgi()),
    
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
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('user/', include([
        path('role/', UserRoleView.as_view(), name='user-role'),
        path('update/', UserUpdateView.as_view(), name='user-update'),
    ])),
    path('business/register/', register_business, name='business-register'),
    path('classes/', include([
        path('images/<int:pk>/', ClassImageDetail.as_view(), name='class-image-detail'),
        path('<int:pk>/reviews/', ClassReviews.as_view(), name='class-reviews'),
        path('<int:pk>/images/', ClassImageList.as_view(), name='class-images'),
        path('<int:pk>/options/', ClassOptionDetail.as_view(), name='class-option-create'),
        path('<int:pk>/options/<int:option_id>/', ClassOptionDetail.as_view(), name='class-option-detail'),
    ])),
    path('reviews/submit/', ReviewSubmission.as_view(), name='submit-review'),
    path('schedule-instances/<int:pk>/', include([
        path('mark-attendance/', 
             ScheduleInstanceViewSet.as_view({'post': 'mark_attendance'}),
             name='mark-attendance'),
        path('cancel/',
             ScheduleInstanceViewSet.as_view({'post': 'cancel'}),
             name='cancel-instance'),
    ])),
    path('revenue/analytics/', RevenueAnalyticsView.as_view(), name='revenue-analytics'),
    path('chat/message/', ChatMessageView.as_view(), name='chat-message'),
    path('payments/webhook/', ProcessBookingWebhook.as_view(), name='payment-webhook'),
    path('payments/create-payment-intent/', CreatePaymentIntentView.as_view(), name='create-payment-intent'),
    path('my_bookings/', BookingViewSet.as_view({'get': 'my_bookings'}), name='my-bookings'),
    path('<int:pk>/student_cancel/', BookingViewSet.as_view({'post': 'student_cancel'}), name='student-cancel'),


    # Platform Management
    path('', include(user_management_router.urls)),

    path('admin/users/<int:pk>/lock/', UserAdminViewSet.as_view({'post': 'lock_account'}), name='admin-lock-user'),
    path('admin/users/<int:pk>/unlock/', UserAdminViewSet.as_view({'post': 'unlock_account'}), name='admin-unlock-user'),
    path('admin/users/<int:pk>/reset-password/', UserAdminViewSet.as_view({'post': 'reset_password'}), name='admin-reset-password'),
    path('admin/users/metrics/', UserAdminViewSet.as_view({'get': 'user_metrics'}), name='admin-user-metrics'),
    
    path('admin/roles/<int:pk>/duplicate/', RoleManagementViewSet.as_view({'post': 'duplicate'}), name='admin-duplicate-role'),
    path('admin/roles/permissions/', RoleManagementViewSet.as_view({'get': 'permissions'}), name='admin-role-permissions'),
    
    path('verification/submit/', VerificationRequestViewSet.as_view({'post': 'submit_verification'}), name='submit-verification'),
    path('verification/<uuid:pk>/process/', VerificationRequestViewSet.as_view({'post': 'process_verification'}), name='process-verification'),
    
    path('admin/audit-logs/export/', AuditLogViewSet.as_view({'get': 'export'}), name='export-audit-logs'),
    path('admin/audit-logs/activity-summary/', AuditLogViewSet.as_view({'get': 'activity_summary'}), name='audit-activity-summary'),
]