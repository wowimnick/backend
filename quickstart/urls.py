from django.urls import path, include, re_path
from django.views.generic import TemplateView
from django.contrib import admin

# --- Import all your existing views and routers ---
from rest_framework.routers import DefaultRouter
from dj_rest_auth.registration.views import VerifyEmailView, ResendEmailVerificationView
from dj_rest_auth.views import PasswordResetConfirmView

from quickstart.views.auth.auth_views import CSRFTokenView
from quickstart.views.auth.social_auth_views import GoogleLogin
from quickstart.payments.booking_status_views import BookingStatusByPaymentIntentView

from quickstart.views.admin.metrics_monitoring.admin_metrics_views import (
    AdminMetricsView,
)
from quickstart.views.admin.notifications.notification_views import (
    AdminNotificationAttachmentViewSet,
    AdminNotificationCampaignViewSet,
    AdminUserSegmentViewSet,
)
from quickstart.views.admin.booking_management.booking_views import AdminBookingViewSet
from quickstart.views.admin.booking_management.payment_views import AdminPaymentViewSet
from quickstart.views.admin.class_management.class_management_views import (
    AdminCategoryViewSet,
    AdminClassViewSet,
    AdminReviewViewSet,
)
from quickstart.payments.views import CreatePaymentIntentView, ProcessBookingWebhook
from quickstart.views.admin.user_management.user_admin_views import UserAdminViewSet
from quickstart.views.admin.user_management.role_views import RoleManagementViewSet
from quickstart.views.admin.user_management.verification_views import (
    VerificationRequestViewSet,
)
from quickstart.views.admin.user_management.audit_views import AuditLogViewSet
from quickstart.views.admin.business_management.business_admin_views import (
    BusinessAdminViewSet,
)
from quickstart.views.admin.support_management.support_management_views import (
    AdminSupportTicketViewSet,
)

from quickstart.views import (
    CustomTokenObtainPairView,
    CustomTokenRefreshView,
    LogoutView,
    UserUpdateView,
    CustomRegisterView,
    get_user_businesses,
    BusinessBookingViewSet,
    StudentBookingViewSet,
    BusinessDashboardViewSet,
    MyBusinessProfileView,
    PublicBusinessInfoViewSet,
    register_business,
    ClassReviews,
    BusinessStudentViewSet,
    MyProfileView,
    RevenueAnalyticsView,
    ReviewSubmission,
    ChatMessageView,
    UserSupportTicketViewSet,
    CreateSupportTicketView,
    BusinessClassViewSet,
    BusinessClassOptionDetail,
    BusinessScheduleViewSet,
    BusinessScheduleInstanceViewSet,
    PublicClassViewSet,
    PublicScheduleViewSet,
    BusinessReviewViewSet,
    MyBusinessOverviewView,
    MyFavoritesListView,
    PublicCategoryViewSet,
    StripeConnectView,
    stripe_connect_webhook,
    NotificationViewSet,
    AllCategoriesForBusinessViewSet,
)

# =============================================================================
# ROUTER DEFINITIONS
# =============================================================================

# --- Public Router ---
public_router = DefaultRouter()
public_router.register(
    r"businesses", PublicBusinessInfoViewSet, basename="public-business"
)
public_router.register(r"classes", PublicClassViewSet, basename="public-class")
public_router.register(r"schedules", PublicScheduleViewSet, basename="public-schedule")
public_router.register(
    r"categories", PublicCategoryViewSet, basename="public-categories"
)

# --- Business Management Router ---
business_management_router = DefaultRouter()
business_management_router.register(
    r"classes", BusinessClassViewSet, basename="business-class"
)
business_management_router.register(
    r"reviews", BusinessReviewViewSet, basename="business-review"
)
business_management_router.register(
    r"bookings", BusinessBookingViewSet, basename="business-booking"
)
business_management_router.register(
    r"students", BusinessStudentViewSet, basename="business-student"
)
business_management_router.register(
    r"schedules", BusinessScheduleViewSet, basename="business-schedule"
)
business_management_router.register(
    r"schedule-instances",
    BusinessScheduleInstanceViewSet,
    basename="business-schedule-instance",
)
business_management_router.register(
    r"notifications", NotificationViewSet, basename="notification"
)

# --- User Self-Service Router ---
user_self_router = DefaultRouter()
user_self_router.register(r"my-bookings", StudentBookingViewSet, basename="my-booking")
user_self_router.register(
    r"support-tickets", UserSupportTicketViewSet, basename="user-support-ticket"
)

# --- Admin Router ---
admin_router = DefaultRouter()
admin_router.register(r"users", UserAdminViewSet, basename="admin-users")
admin_router.register(r"roles", RoleManagementViewSet, basename="admin-roles")
admin_router.register(
    r"verification", VerificationRequestViewSet, basename="admin-verification"
)
admin_router.register(r"audit-logs", AuditLogViewSet, basename="admin-audit-logs")
admin_router.register(r"businesses", BusinessAdminViewSet, basename="admin-businesses")
admin_router.register(r"classes", AdminClassViewSet, basename="admin-classes")
admin_router.register(r"categories", AdminCategoryViewSet, basename="admin-categories")
admin_router.register(r"reviews", AdminReviewViewSet, basename="admin-reviews")
admin_router.register(r"bookings", AdminBookingViewSet, basename="admin-bookings")
admin_router.register(r"payments", AdminPaymentViewSet, basename="admin-payments")
admin_router.register(
    r"support-tickets", AdminSupportTicketViewSet, basename="admin-support-tickets"
)
admin_router.register(
    r"notifications", AdminNotificationCampaignViewSet, basename="admin-notifications"
)
admin_router.register(
    r"user-segments", AdminUserSegmentViewSet, basename="admin-user-segments"
)
admin_router.register(
    r"notification-attachments",
    AdminNotificationAttachmentViewSet,
    basename="admin-notification-attachments",
)

# =============================================================================
# URL PATTERNS
# =============================================================================

# All API endpoints will be prefixed with `api/` by the project's root urls.py
urlpatterns = [
    # --- Django Admin & 3rd Party Libs ---
    path("admin/silk/", include("silk.urls", namespace="admin_silk")),
    path("admin/panel/", admin.site.urls),
    # --- Router Includes ---
    path("admin/", include(admin_router.urls)),
    path("business/", include(business_management_router.urls)),
    path("", include(public_router.urls)),
    path("", include(user_self_router.urls)),
    # --- Standalone Business-Related Views ---
    path(
        "business-stats/",
        BusinessDashboardViewSet.as_view({"get": "list"}),
        name="business-stats-list",
    ),
    path(
        "my-business/overview/",
        MyBusinessOverviewView.as_view(),
        name="my-business-overview",
    ),
    path(
        "business/all-categories/",
        AllCategoriesForBusinessViewSet.as_view({"get": "list"}),
        name="all-categories-for-business",
    ),
    path(
        "business-stats/<int:pk>/",
        BusinessDashboardViewSet.as_view({"get": "retrieve"}),
        name="business-stats-detail",
    ),
    path(
        "business-stats/<int:pk>/dashboard_stats/",
        BusinessDashboardViewSet.as_view({"get": "dashboard_stats"}),
        name="business-stats-dashboard",
    ),
    path(
        "business-stats/<int:pk>/revenue_over_time/",
        BusinessDashboardViewSet.as_view({"get": "revenue_over_time"}),
        name="business-stats-revenue",
    ),
    path(
        "business-stats/<int:pk>/class_performance/",
        BusinessDashboardViewSet.as_view({"get": "class_performance"}),
        name="business-stats-class-perf",
    ),
    path(
        "my-business/stripe-connect/",
        StripeConnectView.as_view(),
        name="stripe-connect-onboarding",
    ),
    path(
        "webhooks/stripe-connect/",
        stripe_connect_webhook,
        name="stripe-connect-webhook",
    ),
    path("business/register/", register_business, name="business-register"),
    path("my-businesses/", get_user_businesses, name="my-businesses"),
    path(
        "my-business/profile/",
        MyBusinessProfileView.as_view(),
        name="my-business-profile",
    ),
    path(
        "business/classes/<int:pk>/options/<int:option_id>/",
        BusinessClassOptionDetail.as_view(),
        name="business-class-option-detail",
    ),
    # --- Authentication Views ---
    path(
        "auth/",
        include(
            [
                path("", include("dj_rest_auth.urls")),
                path("google/", GoogleLogin.as_view(), name="google_login"),
                path(
                    "registration/",
                    include(
                        [
                            path(
                                "", CustomRegisterView.as_view(), name="rest_register"
                            ),
                            path(
                                "verify-email/",
                                VerifyEmailView.as_view(),
                                name="rest_verify_email",
                            ),
                            path(
                                "resend-email/",
                                ResendEmailVerificationView.as_view(),
                                name="rest_resend_email",
                            ),
                            re_path(
                                r"^account-confirm-email/(?P<key>[-:\w]+)/$",
                                VerifyEmailView.as_view(),
                                name="account_confirm_email",
                            ),
                            path(
                                "account-confirm-email/",
                                TemplateView.as_view(),
                                name="account_email_verification_sent",
                            ),
                            re_path(
                                r"^password/reset/confirm/(?P<uidb64>[0-9A-Za-z_\-]+)/(?P<token>[0-9A-Za-z]{1,13}-[0-9A-Za-z]{1,32})/$",
                                PasswordResetConfirmView.as_view(),
                                name="password_reset_confirm",
                            ),
                        ]
                    ),
                ),
            ]
        ),
    ),
    path("login/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("csrf/", CSRFTokenView.as_view(), name="csrf_cookie"),
    path("token/refresh/", CustomTokenRefreshView.as_view(), name="token_refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    # --- Standalone User Self-Service Views ---
    path("user/update/", UserUpdateView.as_view(), name="user-update"),
    path("user/profile/", MyProfileView.as_view(), name="my-profile"),
    path("my-favorites/", MyFavoritesListView.as_view(), name="my-favorites-list"),
    # --- Other Standalone Views ---
    path("admin/metrics/", AdminMetricsView.as_view(), name="admin-metrics"),
    path(
        "classes/<int:pk>/reviews/", ClassReviews.as_view(), name="public-class-reviews"
    ),
    path("reviews/submit/", ReviewSubmission.as_view(), name="submit-review"),
    path(
        "revenue/analytics/", RevenueAnalyticsView.as_view(), name="revenue-analytics"
    ),
    path("chat/message/", ChatMessageView.as_view(), name="chat-message"),
    # --- Payment & Webhook Views ---
    path("payments/webhook/", ProcessBookingWebhook.as_view(), name="payment-webhook"),
    path(
        "booking-status/by-payment-intent/<str:payment_intent_id>/",
        BookingStatusByPaymentIntentView.as_view(),
        name="booking-status-by-payment-intent",
    ),
    path(
        "payments/create-payment-intent/",
        CreatePaymentIntentView.as_view(),
        name="create-payment-intent",
    ),
]
