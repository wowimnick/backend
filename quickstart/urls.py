from django.urls import path, include, re_path
from django.views.generic import TemplateView
from django.contrib import admin
from rest_framework.routers import DefaultRouter
from dj_rest_auth.registration.views import VerifyEmailView, ResendEmailVerificationView
from dj_rest_auth.views import PasswordChangeView
from django.urls import path

from quickstart.views.business.email_branding_preview_views import (
    EmailBrandingPreviewView,
)
from quickstart.views.business.business_location_views import (
    BusinessLocationListCreateView,
    BusinessLocationDetailView,
)
from quickstart.views.widget.widget_config_views import (
    WidgetConfigManagementView,
    WidgetConfigRotateApiKeyView,
    CreateWidgetSubscriptionCheckoutView,
    CreateBillingPortalSessionView,
    CreateWidgetSubscriptionPaymentIntentView,
    WidgetSubscriptionView,
    WidgetSubscriptionCancelView,
    WidgetSubscriptionReactivateView,
    WidgetSubscriptionInvoicesView,
    BusinessWidgetDiagnosticsView,
    CreateUpdatePaymentMethodSetupIntentView,
    SetDefaultPaymentMethodView,
    DefaultPaymentMethodView,
    DetachBusinessPaymentMethodView,
    BusinessAddonsView,
)
from quickstart.views.business.membership_views import (
    MembershipProductListCreateView,
    MembershipProductDetailView,
    MembershipProductSyncStripeView,
    MemberListView,
    MemberDetailView,
    MemberCancelView,
    MemberManualAddView,
    MemberApproveView,
    MemberDeclineView,
)
from quickstart.views.business.contact_views import (
    ContactListView,
    ContactDetailView,
    ContactTimelineView,
    ClientSegmentListCreateView,
    ClientSegmentDetailView,
)
from quickstart.views.business.connect_payout_views import (
    ConnectPayoutSettingsView,
    ConnectPayoutBalanceView,
    ConnectPayoutCreateView,
    ConnectPayoutExternalAccountsView,
)
from quickstart.views.business.email_marketing_addon_views import (
    CancelEmailMarketingAddonView,
    ChangeEmailMarketingTierView,
    CreateEmailMarketingAddonCheckoutView,
    CreateEmailMarketingAddonPaymentIntentView,
    InstantSubscribeEmailMarketingAddonView,
    ReactivateEmailMarketingAddonView,
)
from quickstart.views.webhooks.resend_webhooks import resend_webhook_receiver
from quickstart.views.business.email_marketing_views import (
    MarketingAccountView,
    MarketingAudienceFacetsView,
    MarketingAudiencePreviewView,
    MarketingCampaignDetailView,
    MarketingCampaignListCreateView,
    MarketingCampaignScheduleView,
    MarketingCampaignSendView,
    MarketingCampaignTestSendView,
    MarketingDomainDeleteView,
    MarketingDomainListCreateView,
    MarketingDomainVerifyView,
    MarketingEmailUnsubscribeView,
    MarketingSavedSegmentDetailView,
    MarketingSavedSegmentListCreateView,
    MarketingSenderDetailView,
    MarketingSenderListCreateView,
    MarketingSettingsDetailView,
    MarketingTemplateDetailView,
    MarketingTemplateListCreateView,
)
from quickstart.views.business.email_marketing_workflow_views import (
    MarketingWorkflowDetailView,
    MarketingWorkflowEnrollView,
    MarketingWorkflowEnrollmentListView,
    MarketingWorkflowListCreateView,
)
from quickstart.views.business.scheduling_views import (
    AppointmentSlotsView,
    BusinessTimeOffDetailView,
    BusinessTimeOffListCreateView,
    CalendarConnectionDisconnectView,
    CalendarConnectionListView,
    CalendarOAuthCallbackView,
    CalendarOAuthStartView,
    RecurrenceRuleMaterializeView,
    RecurrenceRuleViewSet,
    ServiceAvailabilityWindowListView,
    SessionEditScopeView,
)
from quickstart.views.admin.payout_management.admin_payout_views import (
    AdminPayoutViewSet,
)
from quickstart.views.admin.blog_management.admin_blog_views import (
    AdminBlogCategoryViewSet,
    AdminBlogPostViewSet,
)
from quickstart.views.admin.support_management.support_ticket_views import (
    AdminSupportTicketViewSet,
)
from quickstart.views.admin.conversation_management.admin_conversation_views import (
    AdminConversationViewSet,
)
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
from quickstart.views.admin.notifications.notification_views import (
    AdminNotificationAttachmentViewSet,
    AdminNotificationCampaignViewSet,
    AdminUserSegmentViewSet,
)
from quickstart.views.admin.booking_management.booking_views import AdminBookingViewSet
from quickstart.views.admin.booking_management.payment_views import AdminPaymentViewSet
from quickstart.views.admin.revenue_stats.revenue_stats_views import (
    PlatformRevenueExportAPIView,
    PlatformRevenueOverviewAPIView,
    PlatformRevenueTimeseriesAPIView,
    PlatformRevenueTopAPIView,
)
from quickstart.views.admin.class_management.class_management_views import (
    AdminCategoryViewSet,
    AdminClassViewSet,
)
from quickstart.payments.views import (
    ProcessBookingWebhook,
)
from quickstart.views.admin.user_management.user_admin_views import UserAdminViewSet
from quickstart.views.admin.user_management.banned_ip_views import BannedIPViewSet
from quickstart.views.admin.user_management.role_views import RoleManagementViewSet
from quickstart.views.admin.user_management.audit_views import AuditLogViewSet
from quickstart.views.admin.business_management.business_admin_views import (
    BusinessAdminViewSet,
)
from quickstart.views.admin.metrics_monitoring.admin_metrics_views import AdminMetricsView
from quickstart.views.admin.widget_subscription_admin_views import (
    AdminWidgetSubscriptionViewSet,
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
    BusinessDashboardViewSet,
    MyBusinessProfileView,
    register_business,
    business_onboarding_state,
    BusinessStudentViewSet,
    RevenueAnalyticsView,
    BusinessClassViewSet,
    BusinessClassOptionDetail,
    BusinessScheduleViewSet,
    BusinessScheduleInstanceViewSet,
    BusinessReviewViewSet,
    MyBusinessOverviewView,
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
    BusinessCourseManagementViewSet,
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
    WidgetPublicPlansView,
    WidgetDiagnosticsView,
    WidgetBookingManageView,
)

# =============================================================================
# ROUTER DEFINITIONS
# =============================================================================

# --- Public Router ---
public_router = DefaultRouter()
public_router.register(
    r"blog/posts", PublicBlogPostViewSet, basename="public-blog-posts"
)
public_router.register(
    r"blog/categories", PublicBlogCategoryViewSet, basename="public-blog-categories"
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
    r"course-management",
    BusinessCourseManagementViewSet,
    basename="business-course-management",
)

business_management_router.register(
    r"recurrence-rules", RecurrenceRuleViewSet, basename="business-recurrence-rules"
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

# --- Admin Router ---
admin_router = DefaultRouter()
admin_router.register(r"users", UserAdminViewSet, basename="admin-users")
admin_router.register(r"roles", RoleManagementViewSet, basename="admin-roles")
admin_router.register(r"audit-logs", AuditLogViewSet, basename="admin-audit-logs")
admin_router.register(r"banned-ips", BannedIPViewSet, basename="admin-banned-ips")
admin_router.register(
    r"support-tickets", AdminSupportTicketViewSet, basename="admin-support-tickets"
)
admin_router.register(
    r"conversations", AdminConversationViewSet, basename="admin-conversations"
)
admin_router.register(r"businesses", BusinessAdminViewSet, basename="admin-businesses")
admin_router.register(r"classes", AdminClassViewSet, basename="admin-classes")
admin_router.register(r"categories", AdminCategoryViewSet, basename="admin-categories")
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
    r"widget-subscriptions",
    AdminWidgetSubscriptionViewSet,
    basename="admin-widget-subscriptions",
)


# =============================================================================
# URL PATTERNS
# =============================================================================

widget_urlpatterns = [
    path("plans/", WidgetPublicPlansView.as_view(), name="widget-public-plans"),
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
    path("bookings/manage/", WidgetBookingManageView.as_view(), name="widget-bookings-manage"),
    path("bookings/", GuestBookingCreateView.as_view(), name="widget-create-booking"),
    path("events/", WidgetEventsView.as_view(), name="widget-events"),
    path("membership-products/", WidgetMembershipProductsView.as_view(), name="widget-membership-products"),
    path("membership-subscribe/", WidgetMembershipSubscribeView.as_view(), name="widget-membership-subscribe"),
    path("membership-status/", WidgetMembershipStatusView.as_view(), name="widget-membership-status"),
    path("diagnostics/", WidgetDiagnosticsView.as_view(), name="widget-diagnostics"),
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
    path("impersonate/", include("impersonate.urls")),
    path("accounts/", include("allauth.urls")),
    # --- Routers ---
    path(
        "admin/revenue/overview/",
        PlatformRevenueOverviewAPIView.as_view(),
        name="admin-revenue-overview",
    ),
    path(
        "admin/revenue/timeseries/",
        PlatformRevenueTimeseriesAPIView.as_view(),
        name="admin-revenue-timeseries",
    ),
    path(
        "admin/revenue/top/",
        PlatformRevenueTopAPIView.as_view(),
        name="admin-revenue-top",
    ),
    path(
        "admin/revenue/export/",
        PlatformRevenueExportAPIView.as_view(),
        name="admin-revenue-export",
    ),
    path("admin/", include(admin_router.urls)),
    path("business/", include(business_management_router.urls)),
    # Payment and booking-status paths must come before catch-all "" includes
    path("payments/webhook/", ProcessBookingWebhook.as_view(), name="payment-webhook"),
    path("", include(public_router.urls)),
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
                    TemplateView.as_view(
                        template_name="account/email_verification_sent.html"
                    ),
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
    path(
        "business/generate-upload-url/",
        generate_presigned_upload_url,
        name="generate-upload-url",
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
    path(
        "webhooks/resend/",
        resend_webhook_receiver,
        name="resend-webhook",
    ),
    path("business/register/", register_business, name="business-register"),
    path(
        "my-business/onboarding-state/",
        business_onboarding_state,
        name="business-onboarding-state",
    ),
    path("my-businesses/", get_user_businesses, name="my-businesses"),
    path(
        "my-business/widget-config/",
        WidgetConfigManagementView.as_view(),
        name="my-business-widget-config",
    ),
    path(
        "my-business/widget-config/rotate-key/",
        WidgetConfigRotateApiKeyView.as_view(),
        name="my-business-widget-config-rotate-key",
    ),
    path(
        "my-business/widget-diagnostics/",
        BusinessWidgetDiagnosticsView.as_view(),
        name="my-business-widget-diagnostics",
    ),
    path(
        "my-business/email-branding/preview/",
        EmailBrandingPreviewView.as_view(),
        name="my-business-email-branding-preview",
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
        "my-business/billing-portal/",
        CreateBillingPortalSessionView.as_view(),
        name="my-business-billing-portal",
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
        "my-business/widget-subscription/detach-payment-method/",
        DetachBusinessPaymentMethodView.as_view(),
        name="my-business-widget-subscription-detach-payment-method",
    ),
    path(
        "my-business/addons/",
        BusinessAddonsView.as_view(),
        name="my-business-addons",
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
        "my-business/contacts/<uuid:contact_id>/timeline/",
        ContactTimelineView.as_view(),
        name="my-business-contact-timeline",
    ),
    path(
        "my-business/client-segments/",
        ClientSegmentListCreateView.as_view(),
        name="my-business-client-segments",
    ),
    path(
        "my-business/client-segments/<uuid:segment_id>/",
        ClientSegmentDetailView.as_view(),
        name="my-business-client-segment-detail",
    ),
    path(
        "my-business/payouts/settings/",
        ConnectPayoutSettingsView.as_view(),
        name="my-business-payout-settings",
    ),
    path(
        "my-business/payouts/balance/",
        ConnectPayoutBalanceView.as_view(),
        name="my-business-payout-balance",
    ),
    path(
        "my-business/payouts/create/",
        ConnectPayoutCreateView.as_view(),
        name="my-business-payout-create",
    ),
    path(
        "my-business/payouts/external-accounts/",
        ConnectPayoutExternalAccountsView.as_view(),
        name="my-business-payout-external-accounts",
    ),
    path(
        "my-business/time-off/",
        BusinessTimeOffListCreateView.as_view(),
        name="my-business-time-off",
    ),
    path(
        "my-business/time-off/<uuid:pk>/",
        BusinessTimeOffDetailView.as_view(),
        name="my-business-time-off-detail",
    ),
    path(
        "my-business/calendar/connections/",
        CalendarConnectionListView.as_view(),
        name="my-business-calendar-connections",
    ),
    path(
        "my-business/calendar/connections/<uuid:pk>/disconnect/",
        CalendarConnectionDisconnectView.as_view(),
        name="my-business-calendar-disconnect",
    ),
    path(
        "my-business/calendar/oauth/<str:provider>/start/",
        CalendarOAuthStartView.as_view(),
        name="my-business-calendar-oauth-start",
    ),
    path(
        "my-business/calendar/oauth/<str:provider>/callback/",
        CalendarOAuthCallbackView.as_view(),
        name="my-business-calendar-oauth-callback",
    ),
    path(
        "business/classes/<int:class_id>/availability-windows/",
        ServiceAvailabilityWindowListView.as_view(),
        name="business-service-availability-windows",
    ),
    path(
        "business/classes/<int:class_id>/appointment-slots/",
        AppointmentSlotsView.as_view(),
        name="business-appointment-slots",
    ),
    path(
        "business/recurrence-rules/<uuid:pk>/materialize/",
        RecurrenceRuleMaterializeView.as_view(),
        name="business-recurrence-materialize",
    ),
    path(
        "business/schedule-instances/<int:pk>/edit-scope/",
        SessionEditScopeView.as_view(),
        name="business-session-edit-scope",
    ),
    path(
        "my-business/marketing/account/",
        MarketingAccountView.as_view(),
        name="my-business-marketing-account",
    ),
    path(
        "my-business/marketing/settings/",
        MarketingSettingsDetailView.as_view(),
        name="my-business-marketing-settings",
    ),
    path(
        "my-business/marketing/templates/",
        MarketingTemplateListCreateView.as_view(),
        name="my-business-marketing-templates",
    ),
    path(
        "my-business/marketing/templates/<uuid:template_id>/",
        MarketingTemplateDetailView.as_view(),
        name="my-business-marketing-template-detail",
    ),
    path(
        "my-business/marketing/senders/",
        MarketingSenderListCreateView.as_view(),
        name="my-business-marketing-senders",
    ),
    path(
        "my-business/marketing/senders/<uuid:sender_id>/",
        MarketingSenderDetailView.as_view(),
        name="my-business-marketing-sender-detail",
    ),
    path(
        "my-business/marketing/domains/",
        MarketingDomainListCreateView.as_view(),
        name="my-business-marketing-domains",
    ),
    path(
        "my-business/marketing/domains/<uuid:domain_id>/verify/",
        MarketingDomainVerifyView.as_view(),
        name="my-business-marketing-domain-verify",
    ),
    path(
        "my-business/marketing/domains/<uuid:domain_id>/",
        MarketingDomainDeleteView.as_view(),
        name="my-business-marketing-domain-detail",
    ),
    path(
        "my-business/marketing/campaigns/",
        MarketingCampaignListCreateView.as_view(),
        name="my-business-marketing-campaigns",
    ),
    path(
        "my-business/marketing/campaigns/<uuid:campaign_id>/",
        MarketingCampaignDetailView.as_view(),
        name="my-business-marketing-campaign-detail",
    ),
    path(
        "my-business/marketing/campaigns/<uuid:campaign_id>/send/",
        MarketingCampaignSendView.as_view(),
        name="my-business-marketing-campaign-send",
    ),
    path(
        "my-business/marketing/campaigns/<uuid:campaign_id>/test-send/",
        MarketingCampaignTestSendView.as_view(),
        name="my-business-marketing-campaign-test-send",
    ),
    path(
        "my-business/marketing/campaigns/<uuid:campaign_id>/schedule/",
        MarketingCampaignScheduleView.as_view(),
        name="my-business-marketing-campaign-schedule",
    ),
    path(
        "my-business/marketing/audience/preview/",
        MarketingAudiencePreviewView.as_view(),
        name="my-business-marketing-audience-preview",
    ),
    path(
        "my-business/marketing/audience/facets/",
        MarketingAudienceFacetsView.as_view(),
        name="my-business-marketing-audience-facets",
    ),
    path(
        "my-business/marketing/segments/",
        MarketingSavedSegmentListCreateView.as_view(),
        name="my-business-marketing-segments",
    ),
    path(
        "my-business/marketing/segments/<uuid:segment_id>/",
        MarketingSavedSegmentDetailView.as_view(),
        name="my-business-marketing-segment-detail",
    ),
    path(
        "my-business/marketing/workflows/",
        MarketingWorkflowListCreateView.as_view(),
        name="my-business-marketing-workflows",
    ),
    path(
        "my-business/marketing/workflows/<uuid:workflow_id>/",
        MarketingWorkflowDetailView.as_view(),
        name="my-business-marketing-workflow-detail",
    ),
    path(
        "my-business/marketing/workflows/<uuid:workflow_id>/enroll/",
        MarketingWorkflowEnrollView.as_view(),
        name="my-business-marketing-workflow-enroll",
    ),
    path(
        "my-business/marketing/workflow-enrollments/",
        MarketingWorkflowEnrollmentListView.as_view(),
        name="my-business-marketing-workflow-enrollments",
    ),
    path(
        "my-business/addons/email-marketing/checkout/",
        CreateEmailMarketingAddonCheckoutView.as_view(),
        name="my-business-addon-email-marketing-checkout",
    ),
    path(
        "my-business/addons/email-marketing/payment-intent/",
        CreateEmailMarketingAddonPaymentIntentView.as_view(),
        name="my-business-addon-email-marketing-payment-intent",
    ),
    path(
        "my-business/addons/email-marketing/instant-subscribe/",
        InstantSubscribeEmailMarketingAddonView.as_view(),
        name="my-business-addon-email-marketing-instant-subscribe",
    ),
    path(
        "my-business/addons/email-marketing/change-tier/",
        ChangeEmailMarketingTierView.as_view(),
        name="my-business-addon-email-marketing-change-tier",
    ),
    path(
        "my-business/addons/email-marketing/cancel/",
        CancelEmailMarketingAddonView.as_view(),
        name="my-business-addon-email-marketing-cancel",
    ),
    path(
        "my-business/addons/email-marketing/reactivate/",
        ReactivateEmailMarketingAddonView.as_view(),
        name="my-business-addon-email-marketing-reactivate",
    ),
    path(
        "public/marketing-email/unsubscribe/<str:token>/",
        MarketingEmailUnsubscribeView.as_view(),
        name="public-marketing-email-unsubscribe",
    ),
    path(
        "my-business/locations/",
        BusinessLocationListCreateView.as_view(),
        name="my-business-locations",
    ),
    path(
        "my-business/locations/<uuid:pk>/",
        BusinessLocationDetailView.as_view(),
        name="my-business-location-detail",
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
    path(
        "revenue/analytics/", RevenueAnalyticsView.as_view(), name="revenue-analytics"
    ),
    path(
        "admin/metrics/",
        AdminMetricsView.as_view(),
        name="admin-metrics",
    ),
    path("widget/v1/", include((widget_urlpatterns, "widget"), namespace="widget-v1")),
    path("health-check/", health_check, name="health-check"),
]
