from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from datetime import timedelta

from django.utils import timezone
from django.db.models.functions import TruncDay
import logging
from quickstart.utils.permissions import (
    IsAuthenticated,
    BasePermission,
    CanAccessUserAdmin,
    CanImpersonateUser,
    CanManageTargetUser,
    user_can_manage,
)
from allauth.account.adapter import get_adapter
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sites.shortcuts import get_current_site
from rest_framework_simplejwt.tokens import RefreshToken
from django.conf import settings
from urllib.parse import urlencode

from quickstart.models import AuditLog, Role, Booking, BusinessInfo
from quickstart.serializers.admin.user_management.admin_serializers import (
    AdminUserListSerializer,
    AdminUserDetailSerializer,
    AdminUserCreateUpdateSerializer,
    AdminUserBookingSerializer,
)
from quickstart.serializers.admin.user_management.audit_serializers import AuditLogSerializer

from quickstart.serializers import CustomUserDetailsSerializer
from quickstart.views.admin.metrics_time_windows import get_admin_metrics_window

User = get_user_model()
logger = logging.getLogger(__name__)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


# --- ViewSet ---
class UserAdminViewSet(viewsets.ModelViewSet):
    pagination_class = StandardResultsSetPagination
    permission_classes = [IsAuthenticated, CanAccessUserAdmin]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["email", "first_name", "last_name", "phone_number"]
    ordering_fields = [
        "first_name",
        "last_name",
        "email",
        "createdAt",
        "last_login",
        "bookings_count",
    ]
    ordering = ["-createdAt"]

    def get_queryset(self):
        queryset = (
            User.objects.select_related("role")
            .prefetch_related("owned_businesses")
            .all()
        )

        role_id = self.request.query_params.get("role_id")
        if role_id:
            role_ids = [r_id for r_id in role_id.split(",") if r_id.isdigit()]
            if role_ids:
                queryset = queryset.filter(role_id__in=role_ids)

        status_filter = self.request.query_params.get("status")
        if status_filter == "active":
            queryset = queryset.filter(is_active=True, last_login__isnull=False)
        elif status_filter == "inactive":
            queryset = queryset.filter(is_active=False)
        elif status_filter == "pending":
            queryset = queryset.filter(is_active=True, last_login__isnull=True)

        queryset = queryset.annotate(bookings_count=Count("bookings", distinct=True))

        booking_count_filter = self.request.query_params.get("booking_count")
        if booking_count_filter == "none":
            queryset = queryset.filter(bookings_count=0)
        elif booking_count_filter == "1_5":
            queryset = queryset.filter(
                bookings_count__gte=1, bookings_count__lte=5
            )
        elif booking_count_filter == "5_plus":
            queryset = queryset.filter(bookings_count__gte=5)
        elif booking_count_filter == "10_plus":
            queryset = queryset.filter(bookings_count__gte=10)

        last_active = self.request.query_params.get("last_active")
        now = timezone.now()
        if last_active == "7d":
            queryset = queryset.filter(last_login__gte=now - timedelta(days=7))
        elif last_active == "30d":
            queryset = queryset.filter(last_login__gte=now - timedelta(days=30))
        elif last_active == "90d_inactive":
            queryset = queryset.filter(
                last_login__isnull=False,
                last_login__lt=now - timedelta(days=90),
            )
        elif last_active == "never":
            queryset = queryset.filter(last_login__isnull=True)

        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return AdminUserListSerializer
        elif self.action in ["create", "update", "partial_update"]:
            return AdminUserCreateUpdateSerializer
        return AdminUserDetailSerializer

    @action(
        detail=True,
        methods=["get"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin],
        pagination_class=StandardResultsSetPagination,
        url_path="bookings",
    )
    def get_user_bookings(self, request, pk=None):
        target_user = self.get_object()
        user_bookings = (
            Booking.objects.filter(user=target_user)
            .select_related(
                "schedule_instance__schedule__option__classId"
            )
            .order_by("-booking_date")
        )

        page = self.paginate_queryset(user_bookings)
        if page is not None:
            serializer = AdminUserBookingSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)

        serializer = AdminUserBookingSerializer(
            user_bookings, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @action(
        detail=False,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin],
        url_path="create-shadow",
    )
    def create_shadow_user(self, request):
        """
        Creates a 'Shadow' user for Concierge Onboarding.
        - Sets a random password.
        - Auto-verifies the email (bypassing allauth checks).
        - Sets is_active=True.
        """
        email = request.data.get("email")
        first_name = request.data.get("first_name")
        last_name = request.data.get("last_name")

        if not email:
            return Response({"detail": "Email is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if user exists
        if User.objects.filter(email__iexact=email).exists():
            return Response({"detail": "User with this email already exists"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from django.utils.crypto import get_random_string
            from allauth.account.models import EmailAddress

            # 1. Create User with random password
            random_password = get_random_string(32)
            user = User.objects.create_user(
                username=email, # Assuming email is username, adjust if needed
                email=email,
                password=random_password,
                first_name=first_name,
                last_name=last_name,
                is_active=True
            )

            # 2. Assign Default Role (e.g., Business Owner or default Student)
            # You might want to pass role_id in request, defaulting here for safety
            try:
                default_role = Role.objects.get(name="Student") 
                user.role = default_role
            except Role.DoesNotExist:
                pass # Fallback to system default logic

            user.save()

            # 3. Manually mark email as verified (Bypass verification email)
            EmailAddress.objects.create(
                user=user,
                email=email,
                verified=True,
                primary=True
            )

            # 4. Audit Log
            self._log_user_action(
                user, 
                "user_create", 
                f"Shadow account created via Concierge Onboarding by {request.user.email}",
                metadata={"type": "shadow_account"}
            )

            return Response(AdminUserDetailSerializer(user).data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error creating shadow user: {e}")
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin],
        url_path="send-handover",
    )
    def send_handover_email(self, request, pk=None):
        """
        Sends the 'Magic Link' (Password Reset Token) to the user to claim their account.
        Uses query parameters to open the frontend Auth Drawer in 'claim' mode.
        """
        user = self.get_object()

        is_business_owner = (
            getattr(user, "role", None)
            and user.role.name == "Business Owner"
        ) or user.owned_businesses.exists()
        if not is_business_owner:
            return Response(
                {"detail": "Handover is only available for business owners."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        recipient_email = (user.email or "").strip()
        if not recipient_email:
            return Response(
                {
                    "detail": "This user has no email address; handover email cannot be sent."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            from quickstart.concierge_handover_tokens import (
                concierge_handover_token_generator,
            )
            from quickstart.utils.email_utils import send_concierge_handover_email

            # Generate Token (concierge-specific generator: no time-based expiry).
            # NOTE: We use the raw PK (user.pk) because CustomPasswordResetConfirmView
            # expects a raw UID, not a base64 encoded one.
            token = concierge_handover_token_generator.make_token(user)
            uid = user.pk

            # Construct Frontend URL with Query Parameters
            # Example: https://classeasily.com/?mode=claim-account&uid=123&token=abc-123
            base = getattr(
                settings, "FRONTEND_BASE_URL", "https://classeasily.com"
            ).rstrip("/")
            query = urlencode(
                {"mode": "claim-account", "uid": str(uid), "token": token}
            )
            claim_url = f"{base}/?{query}"

            # Send Email
            send_concierge_handover_email(user, claim_url)

            self._log_user_action(
                user,
                "user_update",
                f"Concierge Handover email sent to {recipient_email}",
            )

            return Response({"detail": "Handover email sent successfully."})

        except Exception as e:
            logger.error(f"Error sending handover email: {e}")
            return Response(
                {"detail": "Failed to send handover email."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(
        detail=True,
        methods=["get"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin],
        url_path="history",
    )
    def get_user_history(self, request, pk=None):
        """
        Get login and device history for the user from AuditLog
        """
        target_user = self.get_object()
        logs = AuditLog.objects.filter(
            user=target_user,
            action="login"
        ).order_by("-timestamp")[:50] # Limit to last 50 logins

        # Reuse AuditLogSerializer but we could use a lighter one
        serializer = AuditLogSerializer(logs, many=True)
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["get"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin],
        url_path="communications",
    )
    def get_user_communications(self, request, pk=None):
        """
        Get email/notification history for the user from AuditLog (action='notification_sent')
        """
        target_user = self.get_object()
        logs = AuditLog.objects.filter(
            target_user=target_user,
            action="notification_sent"
        ).order_by("-timestamp")

        page = self.paginate_queryset(logs)
        if page is not None:
            serializer = AuditLogSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = AuditLogSerializer(logs, many=True)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.add_customuser"):
            self.permission_denied(
                request, message="You do not have permission to add users."
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        is_super_admin_request = request.user.is_superuser or (
            request.user.role and request.user.role.name == "Super Admin"
        )

        role_id = request.data.get("role")
        if role_id and not is_super_admin_request:
            try:
                target_role = Role.objects.get(pk=role_id)
                if (
                    request.user.role
                    and request.user.role.hierarchy_level <= target_role.hierarchy_level
                ):
                    self.permission_denied(
                        request,
                        message=f"You cannot assign the role '{target_role.name}' due to hierarchy restrictions.",
                    )
            except Role.DoesNotExist:
                return Response(
                    {"detail": "Invalid role specified."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        user = serializer.save()
        self._log_user_action(user, "user_create", "User created by admin")
        headers = self.get_success_headers(AdminUserDetailSerializer(user).data)
        return Response(
            AdminUserDetailSerializer(user).data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    def retrieve(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_customuser"):
            self.permission_denied(
                request, message="You do not have permission to view users."
            )
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", True)
        instance = self.get_object()

        has_profile_change_perm = request.user.has_perm("quickstart.change_customuser")
        has_role_change_perm = request.user.has_perm("quickstart.change_user_role")

        is_changing_profile = any(
            field in request.data
            for field in [
                "first_name", "last_name", "email", "birth_date", "bio",
                "phone_number", "country", "city", "state", "address",
                "zipCode", "avatar",
            ]
        )
        is_changing_role = "role" in request.data and str(
            request.data.get("role")
        ) != str(instance.role_id)

        if is_changing_profile and not has_profile_change_perm:
            self.permission_denied(
                request,
                message="You do not have permission to update user profile fields.",
            )
        if is_changing_role and not has_role_change_perm:
            self.permission_denied(
                request, message="You do not have permission to change user roles."
            )

        if not user_can_manage(request.user, instance):
            self.permission_denied(
                request,
                message="You cannot manage this user due to hierarchy restrictions.",
            )

        is_super_admin_request = request.user.is_superuser or (
            request.user.role and request.user.role.name == "Super Admin"
        )

        if is_changing_role and not is_super_admin_request:
            new_role_id = request.data.get("role")
            if new_role_id:
                try:
                    new_role = Role.objects.get(pk=new_role_id)
                    if (
                        request.user.role
                        and request.user.role.hierarchy_level
                        <= new_role.hierarchy_level
                    ):
                        self.permission_denied(
                            request,
                            message=f"You cannot assign the role '{new_role.name}' due to hierarchy restrictions.",
                        )
                except Role.DoesNotExist:
                    return Response(
                        {"detail": "Invalid new role specified."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        old_role_name = instance.role.name if instance.role else "None"
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        new_role_name = user.role.name if user.role else "None"

        if is_changing_role:
            self._log_user_action(
                user,
                "role_change",
                f"Role changed from {old_role_name} to {new_role_name}",
                metadata={
                    "changes": {"role": {"from": old_role_name, "to": new_role_name}}
                },
            )
        if is_changing_profile and not is_changing_role:
            self._log_user_action(
                user, "user_update", "User profile information updated by admin"
            )

        return Response(AdminUserDetailSerializer(user).data)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        if not request.user.has_perm("quickstart.delete_customuser"):
            self.permission_denied(
                request, message="You do not have permission to delete users."
            )

        if not user_can_manage(request.user, instance):
            self.permission_denied(
                request,
                message="You cannot delete this user due to hierarchy restrictions.",
            )
        if instance == request.user:
            return Response(
                {"detail": "You cannot delete your own account via this interface."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_email = instance.email
        self._log_user_action(
            instance, "user_delete", f"User {user_email} deleted by admin"
        )
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanImpersonateUser],
        url_path="impersonate",
        url_name="impersonate",
    )
    def impersonate(self, request, pk=None):
        admin_user = request.user
        target_user = self.get_object()

        if not target_user.is_active:
            return Response(
                {"detail": "Cannot impersonate an inactive user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            AuditLog.objects.create(
                user=admin_user,
                user_email=admin_user.email,
                action="user_impersonate_start",
                details=f"Admin '{admin_user.email}' started impersonating user '{target_user.email}'.",
                target_user=target_user,
            )
        except Exception as e:
            logger.error("Failed to create impersonation audit log: %s", e)

        refresh = RefreshToken.for_user(target_user)
        refresh["is_impersonated"] = True
        refresh["impersonator_id"] = admin_user.userId
        refresh["impersonator_email"] = admin_user.email

        user_serializer = CustomUserDetailsSerializer(target_user)
        response_data = {"user": user_serializer.data}
        response = Response(response_data, status=status.HTTP_200_OK)

        response.set_cookie(
            settings.SIMPLE_JWT["AUTH_COOKIE"],
            str(refresh.access_token),
            max_age=settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
            secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
        )
        response.set_cookie(
            settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"],
            str(refresh),
            max_age=settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
            secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
        )

        return response

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin, CanManageTargetUser],
    )
    def lock_account(self, request, pk=None):
        user = self.get_object()
        if not request.user.has_perm("quickstart.lock_user"):
            self.permission_denied(
                request,
                message="You do not have permission to lock/unlock user accounts.",
            )

        if not user.is_active:
            return Response(
                {"detail": "Account is already locked."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_active = False
        user.save(update_fields=["is_active"])
        self._log_user_action(user, "account_lock", f"Account locked by admin")
        return Response({"status": "Account locked"})

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin, CanManageTargetUser],
    )
    def unlock_account(self, request, pk=None):
        user = self.get_object()
        if not request.user.has_perm("quickstart.lock_user"):
            self.permission_denied(
                request,
                message="You do not have permission to lock/unlock user accounts.",
            )

        if user.is_active:
            return Response(
                {"detail": "Account is already active."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_active = True
        user.save(update_fields=["is_active"])
        self._log_user_action(user, "account_unlock", f"Account unlocked by admin")
        return Response({"status": "Account unlocked"})

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessUserAdmin, CanManageTargetUser],
    )
    def reset_password(self, request, pk=None):
        user = self.get_object()
        if not request.user.has_perm("quickstart.reset_user_password"):
            self.permission_denied(
                request,
                message="You do not have permission to initiate password resets.",
            )

        if not (user.email or "").strip():
            return Response(
                {"detail": "User has no email address; password reset cannot be sent."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        token = default_token_generator.make_token(user)
        adapter = get_adapter(request)
        password_reset_url = adapter.get_password_reset_url(request, user, token)
        context = {
            "current_site": get_current_site(request),
            "user": user,
            "password_reset_url": password_reset_url,
            "request": request,
        }
        adapter.send_mail("account/email/password_reset_key", user.email, context)

        logger.info(
            f"Password reset email sent for user {user.email} by admin {request.user.email}"
        )
        self._log_user_action(
            user, "password_reset", "Password reset email sent by admin"
        )
        return Response({"status": "Password reset email sent."})

    @action(detail=False, methods=["get"])
    def metrics(self, request):
        if not request.user.has_perm("quickstart.view_user_metrics"):
            self.permission_denied(
                request, message="You do not have permission to view user metrics."
            )

        # 1. Date Range Processing
        window = get_admin_metrics_window(
            request.query_params, default_days=30, logger=logger
        )

        # 2. User Model Aggregations
        user_counts = User.objects.aggregate(
            total_users=Count("userId"), # Total snapshot (not date filtered)
            active_users=Count(
                "userId", filter=Q(is_active=True, last_login__isnull=False)
            ), # Total ever active
            inactive_users=Count("userId", filter=Q(is_active=False)),
            pending_users=Count(
                "userId", filter=Q(is_active=True, last_login__isnull=True)
            ),
            # STRICT DATE FILTERING for "New" metrics
            new_users_in_period=Count(
                "userId",
                filter=Q(
                    createdAt__gte=window.start_dt,
                    createdAt__lt=window.end_dt_exclusive,
                ),
            ),
        )

        # 3. Active User (Engagement) Metric from AuditLog (Strict Date Filter)
        active_users_in_period = (
            AuditLog.objects.filter(
                action="login",
                timestamp__gte=window.start_dt,
                timestamp__lt=window.end_dt_exclusive,
            )
            .values("user_id")
            .distinct()
            .count()
        )

        # 4. Business Accounts Metric (Total)
        total_businesses = BusinessInfo.objects.count()

        # 5. Role and Trend Aggregations
        role_distribution = (
            User.objects.filter(role__isnull=False)
            .values("role__name", "role__color")
            .annotate(count=Count("userId"))
            .order_by("-count")
        )

        # Trend (Strict Date Filter)
        registration_trend = (
            User.objects.filter(
                createdAt__gte=window.start_dt,
                createdAt__lt=window.end_dt_exclusive,
            )
            .annotate(day=TruncDay("createdAt"))
            .values("day")
            .annotate(count=Count("userId"))
            .order_by("day")
        )

        return Response(
            {
                "total_users": user_counts["total_users"],
                "active_users": user_counts["active_users"],
                "inactive_users": user_counts["inactive_users"],
                "pending_users": user_counts["pending_users"],
                "new_users_in_period": user_counts["new_users_in_period"],
                "active_users_in_period": active_users_in_period,
                "business_accounts": total_businesses, # Added metric
                "role_distribution": list(role_distribution),
                "registration_trend": [
                    {
                        "day": item["day"].strftime("%Y-%m-%d"),
                        "registrations": item.get("count", 0),
                    }
                    for item in registration_trend
                ],
                "query_start_date": window.start_date.strftime("%Y-%m-%d"),
                "query_end_date": window.end_date.strftime("%Y-%m-%d"),
            }
        )

    def _log_user_action(self, target_user, action, details, metadata=None):
        requesting_user = getattr(self.request, "user", None)
        if not requesting_user or not requesting_user.is_authenticated:
            return

        try:
            AuditLog.objects.create(
                user=requesting_user,
                user_email=requesting_user.email,
                action=action,
                details=details,
                target_user=target_user,
                target_model="User",
                target_id=str(target_user.userId) if target_user else None,
                ip_address=self.request.META.get("REMOTE_ADDR"),
                user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
                metadata=metadata or {},
            )
        except Exception as e:
            logger.error(
                f"Failed to create audit log: Action={action}, User={requesting_user.email}, Error={str(e)}"
            )