from django.urls import path, include, re_path
from django.views.generic import TemplateView
from django.contrib import admin
from django.shortcuts import get_object_or_404, redirect
from rest_framework.routers import DefaultRouter
from dj_rest_auth.registration.views import VerifyEmailView, ResendEmailVerificationView
from dj_rest_auth.views import PasswordChangeView
from django.urls import path

# --- Model import for the new redirect view ---
from quickstart.views.public.public_class_views import paginated_class_reviews
from quickstart.views.widget.widget_config_views import (
    WidgetConfigManagementView,
    CreateWidgetSubscriptionCheckoutView,
    CreateWidgetSubscriptionPaymentIntentView,
    WidgetSubscriptionView,
    WidgetSubscriptionCancelView,
    WidgetSubscriptionReactivateView,
    WidgetSubscriptionInvoicesView,
    CreateUpdatePaymentMethodSetupIntentView,
    SetDefaultPaymentMethodView,
    DefaultPaymentMethodView,
    BusinessAddonsView,
    CreateMarketplaceEmailAddonCheckoutView,
    CreateMarketplaceEmailAddonPaymentIntentView,
    InstantSubscribeMarketplaceEmailAddonView,
    CancelMarketplaceEmailAddonView,
    ReactivateMarketplaceEmailAddonView,
)
from quickstart.views.business.membership_views import (
    MembershipProductListCreateView,
    MembershipProductDetailView,
    MembershipProductSyncStripeView,
    MemberListView,
    MemberDetailView,
    MemberCancelView,
    MemberPauseView,
    MemberManualAddView,
    MemberApproveView,
    MemberDeclineView,
)
from quickstart.views.business.contact_views import (
    ContactListView,
    ContactDetailView,
)
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
from quickstart.views.admin.conversation_management.admin_conversation_views import (
    AdminConversationViewSet,
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
    AdminCollectionViewSet,
)
from quickstart.payments.views import (
    CreatePaymentIntentView,
    ProcessBookingWebhook,
    UpdatePaymentIntentView,
    CancelPendingBookingView,
    CheckSlotAvailabilityView,
)
from quickstart.views.admin.user_management.user_admin_views import UserAdminViewSet
from quickstart.views.admin.user_management.role_views import RoleManagementViewSet
from quickstart.views.admin.user_management.verification_views import (
    VerificationRequestViewSet,
)
from quickstart.views.admin.user_management.audit_views import AuditLogViewSet
from quickstart.views.admin.business_management.business_admin_views import (
    AdminGeographicalDataView,
    BusinessAdminViewSet,
    ImportGoogleReviewsAdminView,
)
from quickstart.views.admin.global_discount.admin_global_discount_views import (
    AdminGlobalDiscountViewSet,
)
from quickstart.views.admin.widget_subscription_admin_views import (
    AdminWidgetSubscriptionViewSet,
)
from quickstart.views.public.public_global_discount_views import (
    ActiveGlobalDiscountView,
)

from quickstart.views import (
    CustomTokenObtainPairView,
    CustomTokenRefreshView,
    EndImpersonationView,
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
    CreateGiftCardPaymentIntentView,
    ValidateGiftCardView,
    GuestConversationViewSet,
    BusinessConversationViewSet,
)
from quickstart.views.public.guest_conversation_views import (
    GuestMessageCreateView,
    GuestInboxView,
    GuestInboxSendView,
    GuestInboxMarkReadView,
)

from quickstart.views.widget.widget_views import (
    WidgetConfigView,
    WidgetClassListView,
    WidgetAvailabilityView,
    ValidateWidgetCouponView,
    CreateGuestPaymentIntentView,
    GuestBookingCreateView,
    GuestFreeBookingCreateView,
    WidgetEventsView,
    WidgetMembershipProductsView,
    WidgetMembershipSubscribeView,
    WidgetMembershipStatusView,
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
public_router.register(r"courses", PublicCourseViewSet, basename="public-courses")

# --- Business Management Router ---
business_management_router = DefaultRouter()
business_management_router.register(
    r"contact-import", ContactImportViewSet, basename="business-contact-import"
)
business_management_router.register(
    r"classes", BusinessClassViewSet, basename="business-class"
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
business_management_router.register(
    r"conversations", BusinessConversationViewSet, basename="business-conversation"
)

# --- User Self-Service Router ---
user_self_router = DefaultRouter()
user_self_router.register(r"my-bookings", StudentBookingViewSet, basename="my-booking")
user_self_router.register(
    r"support-tickets", UserSupportTicketViewSet, basename="support-ticket"
)
user_self_router.register(
    r"conversations", GuestConversationViewSet, basename="conversation"
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
admin_router.register(
    r"conversations", AdminConversationViewSet, basename="admin-conversations"
)
admin_router.register(r"businesses", BusinessAdminViewSet, basename="admin-businesses")
admin_router.register(r"classes", AdminClassViewSet, basename="admin-classes")
admin_router.register(r"categories", AdminCategoryViewSet, basename="admin-categories")
admin_router.register(
    r"collections", AdminCollectionViewSet, basename="admin-collections"
)
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
admin_router.register(
    r"global-discounts",
    AdminGlobalDiscountViewSet,
    basename="admin-global-discounts",
)
admin_router.register(
    r"widget-subscriptions",
    AdminWidgetSubscriptionViewSet,
    basename="admin-widget-subscriptions",
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
        "validate-coupon/",
        ValidateWidgetCouponView.as_view(),
        name="widget-validate-coupon",
    ),
    path(
        "payment-intent/",
        CreateGuestPaymentIntentView.as_view(),
        name="widget-payment-intent",
    ),
    path("bookings/free/", GuestFreeBookingCreateView.as_view(), name="widget-create-free-booking"),
    path("bookings/", GuestBookingCreateView.as_view(), name="widget-create-booking"),
    path("events/", WidgetEventsView.as_view(), name="widget-events"),
    path("membership-products/", WidgetMembershipProductsView.as_view(), name="widget-membership-products"),
    path("membership-subscribe/", WidgetMembershipSubscribeView.as_view(), name="widget-membership-subscribe"),
    path("membership-status/", WidgetMembershipStatusView.as_view(), name="widget-membership-status"),
]

urlpatterns = [
    path("", health_check, name="api-root-health"),
    # --- Django Admin & 3rd Party Libs ---
    path("admin/panel/", admin.site.urls),
    path(
        "admin/end-impersonation/",
        EndImpersonationView.as_view(),
        name="end_impersonation",
    ),
    path(
        "admin/import-google-reviews/",
        ImportGoogleReviewsAdminView.as_view(),
        name="admin-import-google-reviews",
    ),
    path("impersonate/", include("impersonate.urls")),
    path("accounts/", include("allauth.urls")),
    # --- Routers ---
    path("admin/", include(admin_router.urls)),
    path("business/", include(business_management_router.urls)),
    # Payment and booking-status paths must come before catch-all "" includes
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
        "payments/update-payment-intent/",
        UpdatePaymentIntentView.as_view(),
        name="update-payment-intent",
    ),
    path(
        "payments/update_intent/",
        UpdatePaymentIntentView.as_view(),
        name="update-payment-intent-alt",
    ),
    path(
        "payments/cancel-payment-intent/",
        CancelPendingBookingView.as_view(),
        name="cancel-payment-intent",
    ),
    path(
        "payments/check-slot-availability/",
        CheckSlotAvailabilityView.as_view(),
        name="check-slot-availability",
    ),
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
    # Guest (no-account) messaging: submit from class page, view/reply via token
    path(
        "guest-message/",
        GuestMessageCreateView.as_view(),
        name="guest-message-create",
    ),
    path(
        "guest-inbox/",
        GuestInboxView.as_view(),
        name="guest-inbox",
    ),
    path(
        "guest-inbox/send/",
        GuestInboxSendView.as_view(),
        name="guest-inbox-send",
    ),
    path(
        "guest-inbox/mark-read/",
        GuestInboxMarkReadView.as_view(),
        name="guest-inbox-mark-read",
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
        "classes/homepage-content/",
        PublicClassViewSet.as_view({"get": "homepage_content"}),
        name="public-class-homepage-content",
    ),
    path(
        "classes/<str:pk>/toggle-favorite/",
        PublicClassViewSet.as_view({"post": "toggle_favorite"}),
        name="public-class-toggle-favorite",
    ),
    # Redirect integer IDs before the slug detail view so /classes/123/ redirects, not returns JSON.
    path("classes/<int:class_id>/", class_id_redirect_view, name="class-id-redirect"),
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
        "my-business/widget-subscription/",
        WidgetSubscriptionView.as_view(),
        name="my-business-widget-subscription",
    ),
    path(
        "my-business/widget-subscription/cancel/",
        WidgetSubscriptionCancelView.as_view(),
        name="my-business-widget-subscription-cancel",
    ),
    path(
        "my-business/widget-subscription/reactivate/",
        WidgetSubscriptionReactivateView.as_view(),
        name="my-business-widget-subscription-reactivate",
    ),
    path(
        "my-business/widget-subscription/invoices/",
        WidgetSubscriptionInvoicesView.as_view(),
        name="my-business-widget-subscription-invoices",
    ),
    path(
        "my-business/widget-subscription/checkout/",
        CreateWidgetSubscriptionCheckoutView.as_view(),
        name="my-business-widget-subscription-checkout",
    ),
    path(
        "my-business/widget-subscription/payment-intent/",
        CreateWidgetSubscriptionPaymentIntentView.as_view(),
        name="my-business-widget-subscription-payment-intent",
    ),
    path(
        "my-business/widget-subscription/setup-intent/",
        CreateUpdatePaymentMethodSetupIntentView.as_view(),
        name="my-business-widget-subscription-setup-intent",
    ),
    path(
        "my-business/widget-subscription/set-default-payment-method/",
        SetDefaultPaymentMethodView.as_view(),
        name="my-business-widget-subscription-set-default-payment-method",
    ),
    path(
        "my-business/widget-subscription/default-payment-method/",
        DefaultPaymentMethodView.as_view(),
        name="my-business-widget-subscription-default-payment-method",
    ),
    path(
        "my-business/addons/",
        BusinessAddonsView.as_view(),
        name="my-business-addons",
    ),
    path(
        "my-business/addons/marketplace-email/checkout/",
        CreateMarketplaceEmailAddonCheckoutView.as_view(),
        name="my-business-addon-marketplace-email-checkout",
    ),
    path(
        "my-business/addons/marketplace-email/payment-intent/",
        CreateMarketplaceEmailAddonPaymentIntentView.as_view(),
        name="my-business-addon-marketplace-email-payment-intent",
    ),
    path(
        "my-business/addons/marketplace-email/instant-subscribe/",
        InstantSubscribeMarketplaceEmailAddonView.as_view(),
        name="my-business-addon-marketplace-email-instant-subscribe",
    ),
    path(
        "my-business/addons/marketplace-email/cancel/",
        CancelMarketplaceEmailAddonView.as_view(),
        name="my-business-addon-marketplace-email-cancel",
    ),
    path(
        "my-business/addons/marketplace-email/reactivate/",
        ReactivateMarketplaceEmailAddonView.as_view(),
        name="my-business-addon-marketplace-email-reactivate",
    ),
    path(
        "my-business/membership-products/",
        MembershipProductListCreateView.as_view(),
        name="my-business-membership-products",
    ),
    path(
        "my-business/membership-products/<uuid:product_id>/",
        MembershipProductDetailView.as_view(),
        name="my-business-membership-product-detail",
    ),
    path(
        "my-business/membership-products/<uuid:product_id>/sync-stripe/",
        MembershipProductSyncStripeView.as_view(),
        name="my-business-membership-product-sync-stripe",
    ),
    path(
        "my-business/members/",
        MemberListView.as_view(),
        name="my-business-members",
    ),
    path(
        "my-business/members/manual-add/",
        MemberManualAddView.as_view(),
        name="my-business-members-manual-add",
    ),
    path(
        "my-business/members/<uuid:member_id>/",
        MemberDetailView.as_view(),
        name="my-business-member-detail",
    ),
    path(
        "my-business/members/<uuid:member_id>/cancel/",
        MemberCancelView.as_view(),
        name="my-business-member-cancel",
    ),
    path(
        "my-business/members/<uuid:member_id>/pause/",
        MemberPauseView.as_view(),
        name="my-business-member-pause",
    ),
    path(
        "my-business/members/<uuid:pk>/approve/",
        MemberApproveView.as_view(),
        name="my-business-member-approve",
    ),
    path(
        "my-business/members/<uuid:pk>/decline/",
        MemberDeclineView.as_view(),
        name="my-business-member-decline",
    ),
    path(
        "my-business/contacts/",
        ContactListView.as_view(),
        name="my-business-contacts",
    ),
    path(
        "my-business/contacts/<uuid:contact_id>/",
        ContactDetailView.as_view(),
        name="my-business-contact-detail",
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
    path(
        "gift-cards/purchase-intent/",
        CreateGiftCardPaymentIntentView.as_view(),
        name="gc-purchase",
    ),
    path("gift-cards/validate/", ValidateGiftCardView.as_view(), name="gc-validate"),
    path(
        "global-discount/active/",
        ActiveGlobalDiscountView.as_view(),
        name="active-global-discount",
    ),
    path(
        "admin/geographical-data/",
        AdminGeographicalDataView.as_view(),
        name="admin-geographical-data",
    ),
    path("widget/v1/", include((widget_urlpatterns, "widget"), namespace="widget-v1")),
    path("health-check/", health_check, name="health-check"),
]
