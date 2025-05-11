from django.urls import path, include
from rest_framework.routers import DefaultRouter
from dj_rest_auth.registration.views import VerifyEmailView, ResendEmailVerificationView
from dj_rest_auth.views import PasswordResetConfirmView
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from django.contrib import admin

from .views.admin.metrics_monitoring.admin_metrics_views import AdminMetricsView
from .views.admin.notifications.notification_views import AdminNotificationAttachmentViewSet, AdminNotificationCampaignViewSet, AdminUserSegmentViewSet
from .views.admin.booking_management.booking_views import AdminBookingViewSet
from .views.admin.booking_management.payment_views import AdminPaymentViewSet
from .views.admin.class_management.class_management_views import AdminCategoryViewSet, AdminClassViewSet, AdminReviewViewSet
from .payments.views import CreatePaymentIntentView, ProcessBookingWebhook
from .views.admin.user_management.user_admin_views import UserAdminViewSet
from .views.admin.user_management.role_views import RoleManagementViewSet
from .views.admin.user_management.verification_views import VerificationRequestViewSet
from .views.admin.user_management.audit_views import AuditLogViewSet
from .views.admin.business_management.business_admin_views import BusinessAdminViewSet
from .views.admin.support_management.support_management_views import AdminSupportTicketViewSet

from .views import (
    # Existing views
    CustomTokenObtainPairView, CustomTokenRefreshView,
    LogoutView, UserUpdateView, CustomRegisterView, 
    get_user_businesses, BusinessBookingViewSet, StudentBookingViewSet,
    BusinessDashboardViewSet, MyBusinessProfileView, PublicBusinessInfoViewSet, 
    register_business, ClassReviews, BusinessStudentViewSet, MyProfileView,
    RevenueAnalyticsView,  ReviewSubmission, ChatMessageView, UserSupportTicketViewSet, CreateSupportTicketView,
    BusinessClassViewSet, BusinessClassOptionDetail, BusinessScheduleViewSet, 
    BusinessScheduleInstanceViewSet, BusinessScheduleBreakViewSet, PublicClassViewSet, PublicScheduleViewSet,
    BusinessReviewViewSet, MyBusinessOverviewView, MyFavoritesListView,
)

# Router for Publicly Accessible Read-Only Endpoints (Base: /api/)
public_router = DefaultRouter()
public_router.register(r'businesses', PublicBusinessInfoViewSet, basename='public-business')
public_router.register(r'classes', PublicClassViewSet, basename='public-class')
public_router.register(r'schedules', PublicScheduleViewSet, basename='public-schedule')

# Router for Business Management Endpoints (Base: /api/business/)
# Note: We map specific viewsets here, even if they could fit elsewhere,
# to logically group them under a 'business' path prefix if desired in the future,
# although current paths don't enforce it strictly to maintain URL structure.
business_management_router = DefaultRouter()
business_management_router.register(r'classes', BusinessClassViewSet, basename='business-class')
business_management_router.register(r'reviews', BusinessReviewViewSet, basename='business-review')
business_management_router.register(r'bookings', BusinessBookingViewSet, basename='business-booking')
business_management_router.register(r'students', BusinessStudentViewSet, basename='business-student')
business_management_router.register(r'schedules', BusinessScheduleViewSet, basename='business-schedule')
business_management_router.register(r'schedule-instances', BusinessScheduleInstanceViewSet, basename='business-schedule-instance')
business_management_router.register(r'schedule-breaks', BusinessScheduleBreakViewSet, basename='business-schedule-break')
# Business dashboard stats - keeping original path /api/business-stats/
# No suitable router base to keep this path, will register separately or keep outside router.

# Router for User/Student Self-Service Endpoints (Base: /api/)
user_self_router = DefaultRouter()
# These will need adjustment once those views are properly separated
user_self_router.register(r'my-bookings', StudentBookingViewSet, basename='my-booking')
user_self_router.register(r'support-tickets', UserSupportTicketViewSet, basename='user-support-ticket') # User's own tickets

# Router for Admin Endpoints (Base: /api/admin/)
admin_router = DefaultRouter()
admin_router.register(r'users', UserAdminViewSet, basename='admin-users')
admin_router.register(r'roles', RoleManagementViewSet, basename='admin-roles') # Admin role management
admin_router.register(r'verification', VerificationRequestViewSet, basename='admin-verification')
admin_router.register(r'audit-logs', AuditLogViewSet, basename='admin-audit-logs')
admin_router.register(r'businesses', BusinessAdminViewSet, basename='admin-businesses')
admin_router.register(r'classes', AdminClassViewSet, basename='admin-classes')
admin_router.register(r'categories', AdminCategoryViewSet, basename='admin-categories')
admin_router.register(r'reviews', AdminReviewViewSet, basename='admin-reviews')
admin_router.register(r'bookings', AdminBookingViewSet, basename='admin-bookings')
admin_router.register(r'payments', AdminPaymentViewSet, basename='admin-payments')
admin_router.register(r'support-tickets', AdminSupportTicketViewSet, basename='admin-support-tickets')
admin_router.register(r'notifications', AdminNotificationCampaignViewSet, basename='admin-notifications')
admin_router.register(r'user-segments', AdminUserSegmentViewSet, basename='admin-user-segments')
admin_router.register(r'notification-attachments', AdminNotificationAttachmentViewSet, basename='admin-notification-attachments')


# --- Main URL Patterns ---
urlpatterns = [
    path('admin/silk/', include('silk.urls', namespace='admin_silk')),
    path('admin/panel/', admin.site.urls), # Django admin panel

    # Include Routers - Order can matter if paths overlap, but bases are distinct here
    path('admin/', include(admin_router.urls)), 
    path('business/', include(business_management_router.urls)),
    path('', include(public_router.urls)), 
    path('', include(user_self_router.urls)), 

    # Business Dashboard Stats (Doesn't fit cleanly in routers while keeping path)
    # Registering it separately
    path('business-stats/', BusinessDashboardViewSet.as_view({'get': 'list'}), name='business-stats-list'),
    path('my-business/overview/', MyBusinessOverviewView.as_view(), name='my-business-overview'),
    path('business-stats/<int:pk>/', BusinessDashboardViewSet.as_view({'get': 'retrieve'}), name='business-stats-detail'),
    path('business-stats/<int:pk>/dashboard_stats/', BusinessDashboardViewSet.as_view({'get': 'dashboard_stats'}), name='business-stats-dashboard'),
    path('business-stats/<int:pk>/revenue_over_time/', BusinessDashboardViewSet.as_view({'get': 'revenue_over_time'}), name='business-stats-revenue'),
    path('business-stats/<int:pk>/class_performance/', BusinessDashboardViewSet.as_view({'get': 'class_performance'}), name='business-stats-class-perf'),

    # --- Standalone URL Paths (Not fitting standard router patterns or needing specific paths) ---

    # Authentication
    path('auth/', include([
        # Core dj-rest-auth (login, logout, password reset request/confirm)
        # These will be at /api/auth/login/, /api/auth/password/reset/, etc.
        path('', include('dj_rest_auth.urls')),

        # Registration specific endpoints grouped under /api/auth/registration/
        path('registration/', include([
            # POST /api/auth/registration/ -> Your custom registration view
            path('', CustomRegisterView.as_view(), name='rest_register'),

            # POST /api/auth/registration/verify-email/ -> dj-rest-auth's verification view
            path('verify-email/', VerifyEmailView.as_view(), name='rest_verify_email'),

            # POST /api/auth/registration/resend-email/ -> dj-rest-auth's resend view
            path('resend-email/', ResendEmailVerificationView.as_view(), name='rest_resend_email'),
            re_path(
                r'^account-confirm-email/(?P<key>[-:\w]+)/$',
                VerifyEmailView.as_view(),
                name='account_confirm_email'
            ),
            path(
                'account-confirm-email/', # Or choose a different path if you prefer
                TemplateView.as_view(),   # Renders a simple template
                name='account_email_verification_sent'
            ),
            re_path(
                r'^password/reset/confirm/(?P<uidb64>[0-9A-Za-z_\-]+)/(?P<token>[0-9A-Za-z]{1,13}-[0-9A-Za-z]{1,32})/$',
                PasswordResetConfirmView.as_view(), # Point it to the actual view
                name='password_reset_confirm'      # The required name
            ),

        ])), # End of 'registration/' include

        # Optional: Allauth's confirmation sent page (if useful)
        # path('account-confirm-email/', TemplateView.as_view(template_name="account_confirmation.html"), name='account_email_verification_sent'),

    ])), # End of 'auth/' include
    path('admin/metrics/', AdminMetricsView.as_view(), name='admin-metrics'),
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),

    # User Self-Service
    path('user/update/', UserUpdateView.as_view(), name='user-update'),
    path('user/profile/', MyProfileView.as_view(), name='my-profile'),
    path('my-favorites/', MyFavoritesListView.as_view(), name='my-favorites-list'),

    # Business Management (Standalone)
    path('business/register/', register_business, name='business-register'),
    path('my-businesses/', get_user_businesses, name='my-businesses'), # List user's businesses
    path('my-business/profile/', MyBusinessProfileView.as_view(), name='my-business-profile'), # Manage own profile

    # Class Related (Standalone/Detail)
    # Assuming class image management is part of BusinessClassViewSet actions now
    # path('classes/images/<int:pk>/', ClassImageDetail.as_view(), name='class-image-detail'), # If needed separately
    # path('classes/<int:pk>/images/', ClassImageList.as_view(), name='class-images'), # If needed separately
    path('business/classes/<int:pk>/options/<int:option_id>/', BusinessClassOptionDetail.as_view(), name='business-class-option-detail'), # Specific business option detail

    path('classes/<int:pk>/reviews/', ClassReviews.as_view(), name='public-class-reviews'), 
    path('reviews/submit/', ReviewSubmission.as_view(), name='submit-review'), 

    # Schedule Instance Actions (Business) - These are now actions within BusinessScheduleInstanceViewSet
    # path('schedule-instances/<int:pk>/mark-attendance/', ...),
    # path('schedule-instances/<int:pk>/cancel/', ...),

    # Revenue Analytics (Business)
    path('revenue/analytics/', RevenueAnalyticsView.as_view(), name='revenue-analytics'),

    # Chat (User)
    path('chat/message/', ChatMessageView.as_view(), name='chat-message'),

    # Payments
    path('payments/webhook/', ProcessBookingWebhook.as_view(), name='payment-webhook'),
    path('payments/create-payment-intent/', CreatePaymentIntentView.as_view(), name='create-payment-intent'),

    # Verification (User - Actions are within VerificationRequestViewSet in admin_router, maybe needs user actions?)
    # path('verification/submit/', VerificationRequestViewSet.as_view({'post': 'submit_verification'}), name='submit-verification'), # Example user action if needed

    re_path(r'^.*$', TemplateView.as_view(template_name='index.html')), # Catch-all for frontend routing (React Router)
]