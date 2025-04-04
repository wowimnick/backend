from django.urls import path, include
from rest_framework.routers import DefaultRouter
from allauth.account.views import confirm_email
from django.views.generic import TemplateView


from quickstart.views.notifications.notification_views import AdminNotificationAttachmentViewSet, AdminNotificationCampaignViewSet, AdminUserSegmentViewSet
from quickstart.views.booking_management.booking_views import AdminBookingViewSet
from quickstart.views.booking_management.payment_views import AdminPaymentViewSet
from quickstart.views.class_management.class_management_views import AdminCategoryViewSet, AdminClassViewSet, AdminReviewViewSet
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
    ScheduleBreakViewSet, UserSupportTicketViewSet, CreateSupportTicketView
)

from .views.user_management.user_admin_views import UserAdminViewSet
from .views.user_management.role_views import RoleManagementViewSet
from .views.user_management.verification_views import VerificationRequestViewSet
from .views.user_management.audit_views import AuditLogViewSet
from .views.business_management.business_admin_views import BusinessAdminViewSet
from .views.support_management.support_management_views import AdminSupportTicketViewSet
from .views.support_ticket_views import UserSupportTicketViewSet, CreateSupportTicketView

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
router.register(r'support-tickets', UserSupportTicketViewSet, basename='user-support-tickets')

router.register(r'admin/users', UserAdminViewSet, basename='admin-users')
router.register(r'admin/roles', RoleManagementViewSet, basename='admin-roles')
router.register(r'admin/verification', VerificationRequestViewSet, basename='admin-verification')
router.register(r'admin/audit-logs', AuditLogViewSet, basename='admin-audit-logs')
router.register(r'admin/businesses', BusinessAdminViewSet, basename='admin-businesses')
router.register(r'admin/classes', AdminClassViewSet, basename='admin-classes')
router.register(r'admin/categories', AdminCategoryViewSet, basename='admin-categories')
router.register(r'admin/reviews', AdminReviewViewSet, basename='admin-reviews')
router.register(r'admin/bookings', AdminBookingViewSet, basename='admin-bookings')
router.register(r'admin/payments', AdminPaymentViewSet, basename='admin-payments')
router.register(r'admin/support-tickets', AdminSupportTicketViewSet, basename='admin-support-tickets')
router.register(r'admin/notifications', AdminNotificationCampaignViewSet, basename='admin-notifications')
router.register(r'admin/user-segments', AdminUserSegmentViewSet, basename='admin-user-segments')
router.register(r'admin/notification-attachments', AdminNotificationAttachmentViewSet, basename='admin-notification-attachments')


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
    path('support-tickets/<int:pk>/reply/', UserSupportTicketViewSet.as_view({'post': 'reply'}), name='user-support-ticket-reply'),
    path('support-tickets/summary/', UserSupportTicketViewSet.as_view({'get': 'summary'}), name='user-support-ticket-summary'),
    path('support-tickets/create/', CreateSupportTicketView.as_view(), name='create-support-ticket'),

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

    path('admin/users/<int:pk>/lock/', UserAdminViewSet.as_view({'post': 'lock_account'}), name='admin-lock-user'),
    path('admin/users/<int:pk>/unlock/', UserAdminViewSet.as_view({'post': 'unlock_account'}), name='admin-unlock-user'),
    path('admin/users/<int:pk>/reset-password/', UserAdminViewSet.as_view({'post': 'reset_password'}), name='admin-reset-password'),

    path('admin/roles/<int:pk>/duplicate/', RoleManagementViewSet.as_view({'post': 'duplicate'}), name='admin-duplicate-role'),
    path('admin/roles/permissions/', RoleManagementViewSet.as_view({'get': 'permissions'}), name='admin-role-permissions'),

    path('verification/submit/', VerificationRequestViewSet.as_view({'post': 'submit_verification'}), name='submit-verification'),
    path('verification/<uuid:pk>/process/', VerificationRequestViewSet.as_view({'post': 'process_verification'}), name='process-verification'),
    
    path('admin/audit-logs/export/', AuditLogViewSet.as_view({'get': 'export'}), name='export-audit-logs'),
    path('admin/audit-logs/activity-summary/', AuditLogViewSet.as_view({'get': 'activity_summary'}), name='audit-activity-summary'),

    path('admin/businesses/<int:pk>/toggle-feature/', BusinessAdminViewSet.as_view({'post': 'toggle_feature'}), name='business-toggle-feature'),
    path('admin/businesses/metrics/', BusinessAdminViewSet.as_view({'get': 'metrics'}), name='business-metrics'),
    path('admin/businesses/geographical/', BusinessAdminViewSet.as_view({'get': 'geographical'}), name='business-geographical'),
    path('admin/businesses/export/', BusinessAdminViewSet.as_view({'get': 'export'}), name='business-export'),
    path('admin/businesses/announcements/', BusinessAdminViewSet.as_view({'post': 'announcements'}), name='business-announcements'),
    
    path('admin/classes/analytics/', AdminClassViewSet.as_view({'get': 'analytics'}), name='admin-class-analytics'),
    path('admin/classes/export/', AdminClassViewSet.as_view({'get': 'export'}), name='admin-class-export'),
    path('admin/categories/stats/', AdminCategoryViewSet.as_view({'get': 'stats'}), name='admin-category-stats'),

    path('admin/payments/stats/', AdminPaymentViewSet.as_view({'get': 'stats'}), name='admin-payment-stats'),
    path('admin/payments/<int:pk>/mark-paid/', AdminPaymentViewSet.as_view({'post': 'mark_paid'}), name='admin-mark-payment-paid'),
    path('admin/payments/<int:pk>/history/', AdminPaymentViewSet.as_view({'get': 'history'}), name='admin-payment-history'),
    path('admin/payments/<int:pk>/receipt/', AdminPaymentViewSet.as_view({'get': 'receipt'}), name='admin-payment-receipt'),
    path('admin/payments/export/', AdminPaymentViewSet.as_view({'get': 'export'}), name='admin-export-payments'),
    path('admin/bookings/analytics/', AdminBookingViewSet.as_view({'get': 'analytics'}), name='admin-booking-analytics'),
    path('admin/bookings/export/', AdminBookingViewSet.as_view({'get': 'export'}), name='admin-export-bookings'),
    path('admin/bookings/<int:pk>/cancel/', AdminBookingViewSet.as_view({'post': 'cancel'}), name='admin-cancel-booking'),

    path('admin/support-tickets/<int:pk>/reply/', AdminSupportTicketViewSet.as_view({'post': 'reply'}), name='admin-support-ticket-reply'),
    path('admin/support-tickets/<int:pk>/assign/', AdminSupportTicketViewSet.as_view({'post': 'assign'}), name='admin-support-ticket-assign'),
    path('admin/support-tickets/<int:pk>/resolve/', AdminSupportTicketViewSet.as_view({'post': 'resolve'}), name='admin-support-ticket-resolve'),
    path('admin/support-tickets/<int:pk>/close/', AdminSupportTicketViewSet.as_view({'post': 'close'}), name='admin-support-ticket-close'),
    path('admin/support-tickets/stats/', AdminSupportTicketViewSet.as_view({'get': 'stats'}), name='admin-support-ticket-stats'),
    path('admin/support-tickets/export/', AdminSupportTicketViewSet.as_view({'get': 'export'}), name='admin-support-ticket-export'),

    path('admin/notifications/<uuid:pk>/send/', AdminNotificationCampaignViewSet.as_view({'post': 'send'}), name='admin-notification-send'),
    path('admin/notifications/<uuid:pk>/cancel/', AdminNotificationCampaignViewSet.as_view({'post': 'cancel'}), name='admin-notification-cancel'),
    path('admin/notifications/<uuid:pk>/duplicate/', AdminNotificationCampaignViewSet.as_view({'post': 'duplicate'}), name='admin-notification-duplicate'),
    path('admin/notifications/metrics/', AdminNotificationCampaignViewSet.as_view({'get': 'metrics'}), name='admin-notification-metrics'),
    path('admin/user-segments/<uuid:pk>/users/', AdminUserSegmentViewSet.as_view({'get': 'users'}), name='admin-user-segment-users'),
]