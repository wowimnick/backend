from django.urls import path, include, re_path
from django.views.generic import TemplateView
from django.contrib import admin
from django.shortcuts import get_object_or_404, redirect
from rest_framework.routers import DefaultRouter
from dj_rest_auth.registration.views import VerifyEmailView, ResendEmailVerificationView
from dj_rest_auth.views import PasswordChangeView

# --- Model import for the new redirect view ---
from quickstart.views.public.public_class_views import paginated_class_reviews
from quickstart.views.widget.widget_config_views import WidgetConfigManagementView
from quickstart.views.admin.payout_management.admin_payout_views import (
    AdminPayoutViewSet,
)
from quickstart.views.admin.blog_management.admin_blog_views import (
    AdminBlogCategoryViewSet,
    AdminBlogPostViewSet,
)
from quickstart.models import ClassesMain, BusinessInfo

from quickstart.views.admin.support_management.support_ticket_views import (
    AdminSupportTicketViewSet,
)
from quickstart.views.public.user_support_views import UserSupportTicketViewSet
from quickstart.views.business.business_management_views import (
    generate_presigned_upload_url,
)
from quickstart.views.healthcheck import health_check
from quickstart.views.auth.auth_views import (
    CSRFTokenView,
    CustomPasswordResetView,
    CustomPasswordResetConfirmView,
)
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
    AdminGeographicalDataView,
    BusinessAdminViewSet,
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
    PlatformClassReviews,
    ImportedGoogleReviewsView,
    BusinessStudentViewSet,
    MyProfileView,
    RevenueAnalyticsView,
    ReviewSubmission,
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
    PublicBlogPostViewSet,
    PublicBlogCategoryViewSet,
    BusinessDiscountViewSet,
    BusinessPayoutViewSet,
    BusinessStaffViewSet,
    AcceptStaffInvitationView,
    BusinessRoleViewSet,
    ValidateInvitationTokenView,
    ContactImportViewSet,
    GuestBookingCancellationView,
    PublicCourseViewSet,
    StudentCourseEnrollmentViewSet,
    BusinessCourseManagementViewSet,
)

from quickstart.views.widget.widget_views import (
    WidgetConfigView,
    WidgetClassListView,
    WidgetAvailabilityView,
    CreateGuestPaymentIntentView,
    GuestBookingCreateView,
)

# =============================================================================
# ROUTER DEFINITIONS
# =============================================================================

# --- Public Router ---
public_router = DefaultRouter()
public_router.register(
    r"businesses", PublicBusinessInfoViewSet, basename="public-business"
)
public_router.register(
    r"blog/posts", PublicBlogPostViewSet, basename="public-blog-posts"
)
public_router.register(
    r"blog/categories", PublicBlogCategoryViewSet, basename="public-blog-categories"
)
public_router.register(r"schedules", PublicScheduleViewSet, basename="public-schedule")
public_router.register(
    r"categories", PublicCategoryViewSet, basename="public-categories"
)

# --- Business Management Router ---
business_management_router = DefaultRouter()
business_management_router.register(
    r"contact-import", ContactImportViewSet, basename="business-contact-import"
)
business_management_router.register(
    r"classes", BusinessClassViewSet, basename="business-class"
)
business_management_router.register(
    r"courses", PublicCourseViewSet, basename="public-courses"
)
business_management_router.register(
    r"student/course-enrollments",
    StudentCourseEnrollmentViewSet,
    basename="student-course-enrollments",
)

business_management_router.register(
    r"course-management",
    BusinessCourseManagementViewSet,
    basename="business-course-management",
)

business_management_router.register(
    r"discounts", BusinessDiscountViewSet, basename="business-discount"
)
business_management_router.register(
    r"staff", BusinessStaffViewSet, basename="business-staff"
)
business_management_router.register(
    r"roles", BusinessRoleViewSet, basename="business-role"
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
    r"payouts", BusinessPayoutViewSet, basename="business-payout"
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
    r"support-tickets", UserSupportTicketViewSet, basename="support-ticket"
)

# --- Admin Router ---
admin_router = DefaultRouter()
admin_router.register(r"users", UserAdminViewSet, basename="admin-users")
admin_router.register(r"roles", RoleManagementViewSet, basename="admin-roles")
admin_router.register(
    r"verification", VerificationRequestViewSet, basename="admin-verification"
)
admin_router.register(r"audit-logs", AuditLogViewSet, basename="admin-audit-logs")
admin_router.register(
    r"support-tickets", AdminSupportTicketViewSet, basename="admin-support-tickets"
)
admin_router.register(r"businesses", BusinessAdminViewSet, basename="admin-businesses")
admin_router.register(r"classes", AdminClassViewSet, basename="admin-classes")
admin_router.register(r"categories", AdminCategoryViewSet, basename="admin-categories")
admin_router.register(r"reviews", AdminReviewViewSet, basename="admin-reviews")
admin_router.register(r"bookings", AdminBookingViewSet, basename="admin-bookings")
admin_router.register(r"payments", AdminPaymentViewSet, basename="admin-payments")
admin_router.register(r"payouts", AdminPayoutViewSet, basename="admin-payouts")
admin_router.register(r"blog/posts", AdminBlogPostViewSet, basename="admin-blog-posts")
admin_router.register(
    r"blog/categories", AdminBlogCategoryViewSet, basename="admin-blog-categories"
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


def class_id_redirect_view(request, class_id):
    """
    Permanently redirects an old ID-based browser URL to the new slug-based URL.
    e.g., /classes/123 -> /classes/new-york-pottery-class
    """
    klass = get_object_or_404(ClassesMain, pk=class_id)
    if klass.slug:
        # --- FIX: Redirect to the frontend's canonical PLURAL /classes/ path ---
        return redirect(f"/classes/{klass.slug}", permanent=True)
    # Fallback if a slug doesn't exist for some reason.
    return redirect("/")


def business_id_redirect_view(request, business_id):
    """
    Permanently redirects an old ID-based URL (/business/123/) to the
    new slug-based URL (/business/my-cool-business/).
    """
    business = get_object_or_404(BusinessInfo, pk=business_id)
    if business.slug:
        # Redirect to the frontend's canonical path
        return redirect(f"/business/{business.slug}", permanent=True)
    # Fallback if a slug doesn't exist for some reason.
    return redirect("/")


# =============================================================================
# URL PATTERNS
# =============================================================================

widget_urlpatterns = [
    path("config/", WidgetConfigView.as_view(), name="widget-config"),
    path("classes/", WidgetClassListView.as_view(), name="widget-classes"),
    path("availability/", WidgetAvailabilityView.as_view(), name="widget-availability"),
    path(
        "payment-intent/",
        CreateGuestPaymentIntentView.as_view(),
        name="widget-payment-intent",
    ),
    path("bookings/", GuestBookingCreateView.as_view(), name="widget-create-booking"),
]

urlpatterns = [
    # --- Django Admin & 3rd Party Libs ---
    path("admin/silk/", include("silk.urls", namespace="admin_silk")),
    path("admin/panel/", admin.site.urls),
    path("impersonate/", include("impersonate.urls")),
    path("accounts/", include("allauth.urls")),
    # --- Routers ---
    path("admin/", include(admin_router.urls)),
    path("business/", include(business_management_router.urls)),
    path("", include(public_router.urls)),
    path("", include(user_self_router.urls)),
    # --- Authentication & User Management ---
    path("login/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", CustomTokenRefreshView.as_view(), name="token_refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("csrf/", CSRFTokenView.as_view(), name="csrf_cookie"),
    path(
        "bookings/guest-cancel/<uuid:token>/",
        GuestBookingCancellationView.as_view(),
        name="guest-booking-cancel",
    ),
    # Registration and Email Verification
    path(
        "auth/registration/",
        include(
            [
                path("", CustomRegisterView.as_view(), name="rest_register"),
                path(
                    "verify-email/", VerifyEmailView.as_view(), name="rest_verify_email"
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
            ]
        ),
    ),
    path(
        "auth/password/reset/",
        CustomPasswordResetView.as_view(),
        name="rest_password_reset",
    ),
    path(
        "auth/password/reset/confirm/",
        CustomPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("auth/", include("dj_rest_auth.urls")),
    # Social Auth
    path("auth/google/", GoogleLogin.as_view(), name="google_login"),
    # User Profile Management
    path("user/update/", UserUpdateView.as_view(), name="user-update"),
    path("user/profile/", MyProfileView.as_view(), name="my-profile"),
    path("my-favorites/", MyFavoritesListView.as_view(), name="my-favorites-list"),
    path(
        "classes/search/",
        PublicClassViewSet.as_view({"get": "search"}),
        name="public-class-search",
    ),
    path(
        "classes/<str:pk>/toggle-favorite/",
        PublicClassViewSet.as_view({"post": "toggle_favorite"}),
        name="public-class-toggle-favorite",
    ),
    path(
        "classes/<str:pk>/",
        PublicClassViewSet.as_view({"get": "retrieve"}),
        name="public-class-detail",
    ),
    # The list view (for API consumers, not directly for a page)
    path(
        "classes/",
        PublicClassViewSet.as_view({"get": "list"}),
        name="public-class-list",
    ),
    # This path will capture old /classes/123 style URLs and permanently redirect them.
    # It will not be used by the React router.
    path("classes/<int:class_id>/", class_id_redirect_view, name="class-id-redirect"),
    # --- Other Application Views (Original order maintained) ---
    path(
        "business/<int:business_id>/",
        business_id_redirect_view,
        name="business-id-redirect",
    ),
    path(
        "business/generate-upload-url/",
        generate_presigned_upload_url,
        name="generate-upload-url",
    ),
    path("admin/metrics/", AdminMetricsView.as_view(), name="admin-metrics"),
    path(
        "classes/<str:identifier>/reviews/",
        paginated_class_reviews,
        name="class-reviews-paginated",
    ),
    path(
        "business/<int:business_id>/google-reviews/",
        ImportedGoogleReviewsView.as_view(),
        name="business-google-reviews",
    ),
    path(
        "business-stats/",
        BusinessDashboardViewSet.as_view({"get": "list"}),
        name="business-stats-list",
    ),
    path(
        "business/validate-invitation/",
        ValidateInvitationTokenView.as_view(),
        name="validate-staff-invitation",
    ),
    path(
        "business/accept-invitation/",
        AcceptStaffInvitationView.as_view(),
        name="accept-staff-invitation",
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
        "my-business/widget-config/",
        WidgetConfigManagementView.as_view(),
        name="my-business-widget-config",
    ),
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
    path("reviews/submit/", ReviewSubmission.as_view(), name="submit-review"),
    path(
        "revenue/analytics/", RevenueAnalyticsView.as_view(), name="revenue-analytics"
    ),
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
    path(
        "admin/geographical-data/",
        AdminGeographicalDataView.as_view(),
        name="admin-geographical-data",
    ),
    path("widget/v1/", include((widget_urlpatterns, "widget"), namespace="widget-v1")),
    path("health-check/", health_check, name="health-check"),
]
