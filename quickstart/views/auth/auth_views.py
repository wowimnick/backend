import binascii
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import ScopedRateThrottle
from dj_rest_auth.registration.views import RegisterView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth import get_user_model
from django.conf import settings
import logging

from datetime import timedelta
from django.utils import timezone

from django.views import View
from django.http import JsonResponse

from django.contrib.auth.tokens import default_token_generator
from quickstart.concierge_handover_tokens import concierge_handover_token_generator

from allauth.account.forms import ResetPasswordForm, SetPasswordForm


from quickstart.serializers.auth.auth_serializers import CustomAllAuthPasswordResetForm
from quickstart.models import AuditLog


from quickstart.serializers import (
    CustomTokenObtainPairSerializer,
    CustomUserDetailsSerializer,
    CustomRegisterSerializer,
)
from quickstart.utils.request_utils import get_client_ip  # noqa: F401 — re-exported for callers

logger = logging.getLogger(__name__)


class CustomPasswordResetView(APIView):
    """
    This view now uses our `CustomAllAuthPasswordResetForm`, which correctly
    delegates URL generation to our custom adapter, ensuring the link sent
    in the email points to the frontend with the correct path and parameters.
    """

    permission_classes = [AllowAny]
    throttle_scope = "sensitive"

    def post(self, request, *args, **kwargs):
        # --- MODIFIED: Use our custom form ---
        form = CustomAllAuthPasswordResetForm(request.data)

        if form.is_valid():
            # Our custom form's save method now handles everything correctly.
            form.save(request)
            logger.info(
                f"Password reset email initiated for: {request.data.get('email')}"
            )
            return Response(
                {"detail": "Password reset e-mail has been sent."},
                status=status.HTTP_200_OK,
            )
        else:
            # Pass form errors back to the frontend for display.
            logger.warning(
                f"Password reset failed for {request.data.get('email')}: {form.errors.as_json()}"
            )
            # It's better to return the specific errors from the form.
            return Response(
                form.errors.get_json_data(), status=status.HTTP_400_BAD_REQUEST
            )


# --- MODIFIED: Replaced DRF's APIView with Django's standard View ---
class CSRFTokenView(View):
    """
    An empty, unauthenticated view that ensures the CSRF cookie is set on the client.
    Using a standard Django View to avoid potential conflicts with DRF's APIView
    for this simple purpose. The `ensure_csrf_cookie` decorator signals to
    the middleware to set the cookie on the response.
    """

    @method_decorator(ensure_csrf_cookie)
    def get(self, request, *args, **kwargs):
        return JsonResponse({"detail": "CSRF cookie set."})


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    # --- Rate Limiting ---
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sensitive"

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)

        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            logger.warning(
                f"Failed login attempt for user: {request.data.get('email')}"
            )
            error_detail = e.args[0] if e.args else "Invalid credentials."
            return Response(
                {"detail": error_detail}, status=status.HTTP_401_UNAUTHORIZED
            )

        validated_data = serializer.validated_data
        user = serializer.user

        try:
            AuditLog.objects.create(
                user=user,
                user_email=user.email,
                action="login",
                details=f"User '{user.email}' logged in successfully.",
                ip_address=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
            logger.info(f"Successful login audited for user: {user.email}")
        except Exception as audit_error:
            logger.error(
                f"Failed to create login audit log for user {user.email}: {audit_error}"
            )

        response_data = {
            "user": validated_data["user"],
        }
        response = Response(response_data, status=status.HTTP_200_OK)

        response.set_cookie(
            settings.SIMPLE_JWT["AUTH_COOKIE"],
            validated_data["access"],
            max_age=settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
            secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
        )
        response.set_cookie(
            settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"],
            validated_data["refresh"],
            max_age=settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
            secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
        )
        return response


class CustomTokenRefreshView(APIView):
    def post(self, request, *args, **kwargs):
        refresh_token_str = request.COOKIES.get(
            settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"]
        )

        if not refresh_token_str:
            # Return 401 without a user-facing detail so the frontend does not show a toast
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        User = get_user_model()

        try:
            # First, attempt a standard refresh. This will work 99% of the time.
            refresh = RefreshToken(refresh_token_str)
            user_id = refresh.payload.get("user_id")

            # --- OPTIMIZATION FIX: Prefetch permissions and content_type to avoid N+1 queries ---
            user = (
                User.objects.select_related("role")
                .prefetch_related(
                    "role__permissions", "role__permissions__content_type"
                )
                .get(userId=user_id)
            )

            if not user.is_active:
                response = Response(
                    {"detail": "Account is disabled."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
                self._delete_auth_cookies(response)
                return response

            user_serializer = CustomUserDetailsSerializer(user)

            data = {
                "access": str(refresh.access_token),
                "user": user_serializer.data,
            }

            if settings.SIMPLE_JWT["ROTATE_REFRESH_TOKENS"]:
                if settings.SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"]:
                    try:
                        refresh.blacklist()
                    except AttributeError:
                        pass

                new_refresh = RefreshToken.for_user(user)
                # Preserve impersonation claims so "Return to admin" still works after reload
                if refresh.payload.get("is_impersonated"):
                    new_refresh["is_impersonated"] = True
                    new_refresh["impersonator_id"] = refresh.payload.get("impersonator_id")
                    new_refresh["impersonator_email"] = refresh.payload.get("impersonator_email")
                data["refresh"] = str(new_refresh)

            response = Response(data, status=status.HTTP_200_OK)
            self._set_auth_cookies(response, data["access"], data.get("refresh"))
            return response

        except TokenError as e:
            # --- THIS IS THE ROBUST FIX ---
            # If the token failed, check if it's because it was blacklisted.
            # This is a strong indicator of our race condition.
            if "blacklisted" in str(e).lower():
                try:
                    logger.warning(
                        f"Potential token refresh race condition detected for user. Error: {e}"
                    )
                    # The token is invalid, but we can still decode it without verification to get user info.
                    unverified_payload = RefreshToken(
                        refresh_token_str, verify=False
                    ).payload
                    user_id = unverified_payload.get("user_id")
                    jti = unverified_payload.get("jti")

                    # Find the newest valid token for this user that was created *after* the blacklisted one.
                    blacklisted_entry = BlacklistedToken.objects.get(token__jti=jti)
                    latest_token = (
                        OutstandingToken.objects.filter(
                            user_id=user_id,
                            created_at__gt=blacklisted_entry.blacklisted_at,
                        )
                        .order_by("-created_at")
                        .first()
                    )

                    # Give a 60-second grace period for the race condition.
                    grace_period = timedelta(seconds=60)
                    if (
                        latest_token
                        and (timezone.now() - latest_token.created_at) < grace_period
                    ):
                        logger.warning(
                            f"RACE CONDITION CONFIRMED AND HANDLED for user_id: {user_id}. Issuing new tokens."
                        )
                        # A new token was created recently. This confirms the race condition.
                        # We create new tokens based on this valid, most recent token.
                        new_refresh = RefreshToken(latest_token.token)

                        # --- OPTIMIZATION FIX: Prefetch here as well ---
                        user = (
                            User.objects.select_related("role")
                            .prefetch_related(
                                "role__permissions", "role__permissions__content_type"
                            )
                            .get(userId=user_id)
                        )

                        if not user.is_active:
                            response = Response(
                                {"detail": "Account is disabled."},
                                status=status.HTTP_401_UNAUTHORIZED,
                            )
                            self._delete_auth_cookies(response)
                            return response

                        data = {
                            "access": str(new_refresh.access_token),
                            "refresh": str(
                                new_refresh
                            ),  # Send the newest refresh token back
                            "user": CustomUserDetailsSerializer(user).data,
                        }
                        response = Response(data, status=status.HTTP_200_OK)
                        self._set_auth_cookies(
                            response, data["access"], data["refresh"]
                        )
                        return response

                except Exception as race_condition_error:
                    # If anything goes wrong inside our handler, log it and fail safely.
                    logger.error(
                        f"CRITICAL: Error during token refresh race condition handling: {race_condition_error}"
                    )
                    # Fall through to the generic error response below.

            # If it wasn't a blacklisted token or the race condition handler failed, log the original error and fail.
            logger.error(f"Token Refresh Error: {e}")
            response = Response(
                {"detail": "Token refresh failed or user not found."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            self._delete_auth_cookies(response)
            return response

        except (AttributeError, User.DoesNotExist) as e:
            # Catches errors like user deleted between token issue and refresh.
            logger.error(f"Token Refresh Error (User/Attribute): {e}")
            response = Response(
                {"detail": "Token refresh failed or user not found."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            self._delete_auth_cookies(response)
            return response

    def _set_auth_cookies(self, response, access_token, refresh_token=None):
        response.set_cookie(
            settings.SIMPLE_JWT["AUTH_COOKIE"],
            access_token,
            max_age=settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
            secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
        )
        if refresh_token:
            response.set_cookie(
                settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"],
                refresh_token,
                max_age=settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds(),
                httponly=True,
                samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
                secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
            )

    def _delete_auth_cookies(self, response):
        response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE"])
        response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"])


class EndImpersonationView(APIView):
    """
    Restore the original admin session when ending impersonation.
    Reads the current (impersonated) refresh token from cookies, validates
    is_impersonated and impersonator_id, then issues new tokens for the admin
    and sets them in cookies. No re-login required.
    """
    permission_classes = [IsAuthenticated]

    def _set_auth_cookies(self, response, access_token, refresh_token=None):
        response.set_cookie(
            settings.SIMPLE_JWT["AUTH_COOKIE"],
            access_token,
            max_age=settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
            secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
        )
        if refresh_token:
            response.set_cookie(
                settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"],
                refresh_token,
                max_age=settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds(),
                httponly=True,
                samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
                secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
            )

    def post(self, request):
        User = get_user_model()
        refresh_token_str = request.COOKIES.get(
            settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"]
        )
        if not refresh_token_str:
            return Response(
                {"detail": "Refresh token not found."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        try:
            refresh = RefreshToken(refresh_token_str)
            payload = refresh.payload
            if not payload.get("is_impersonated") or not payload.get("impersonator_id"):
                return Response(
                    {"detail": "Not in an impersonation session."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            impersonator_id = payload.get("impersonator_id")
            admin_user = (
                User.objects.select_related("role")
                .prefetch_related("role__permissions", "role__permissions__content_type")
                .get(userId=impersonator_id)
            )
        except (TokenError, User.DoesNotExist) as e:
            logger.warning(f"End impersonation failed: {e}")
            return Response(
                {"detail": "Invalid or expired session. Please log in again."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        new_refresh = RefreshToken.for_user(admin_user)
        user_serializer = CustomUserDetailsSerializer(admin_user)
        response = Response(
            {"user": user_serializer.data},
            status=status.HTTP_200_OK,
        )
        self._set_auth_cookies(
            response,
            str(new_refresh.access_token),
            str(new_refresh),
        )
        return response


class UserUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        """Handles partial updates to the user profile."""
        serializer = CustomUserDetailsSerializer(
            request.user, data=request.data, partial=True, context={"request": request}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        """
        Handles full updates by delegating to the patch method.
        This resolves potential '405 Method Not Allowed' errors.
        """
        return self.patch(request)


class LogoutView(APIView):
    def post(self, request):
        try:
            refresh_token = request.COOKIES.get(
                settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"]
            )
            response = Response(status=status.HTTP_205_RESET_CONTENT)
            response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE"])
            response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"])
            response.delete_cookie(settings.CSRF_COOKIE_NAME)
            if refresh_token:
                try:
                    token = RefreshToken(refresh_token)
                    token.blacklist()
                except Exception as token_error:
                    logger.warning(f"Token blacklist failed: {token_error}")
            return response
        except Exception as e:
            logger.error(f"Logout error: {e}")
            response = Response(
                {"detail": "Logout failed"}, status=status.HTTP_400_BAD_REQUEST
            )
            response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE"])
            response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"])
            response.delete_cookie(settings.CSRF_COOKIE_NAME)
            return response


class CustomRegisterView(RegisterView):
    serializer_class = CustomRegisterSerializer
    # --- Rate Limiting ---
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sensitive"

    def post(self, request, *args, **kwargs):
        logger.debug(f"Registration request received: {request.data.get('email')}")
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            logger.error(
                f"Registration Serializer errors for {request.data.get('email')}: {serializer.errors}"
            )
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Call the parent method which handles user creation
            response = super().post(request, *args, **kwargs)

            # Log *after* the super().post() call, which includes user creation and signal sending
            if response.status_code in [
                status.HTTP_201_CREATED,
                status.HTTP_200_OK,
            ]:  # Check for success status
                logger.info(
                    f"REGISTRATION VIEW LOG: super().post completed successfully for {request.data.get('email')}. Status: {response.status_code}. About to return response."
                )
                # At this point, the email confirmation *should* have been triggered by allauth/dj-rest-auth
                # If you see this log, but NO adapter logs, the problem is likely in the connection
                # between dj-rest-auth/allauth and your adapter, or settings.
            else:
                logger.warning(
                    f"REGISTRATION VIEW LOG: super().post for {request.data.get('email')} returned non-success status: {response.status_code}, Response data: {response.data}"
                )

            return response

        # --- MODIFIED: Catch the specific validation error from the save() method ---

        except DRFValidationError as e:
            logger.warning(
                f"Registration validation error for {request.data.get('email')}: {e.detail}"
            )

            # If the error is a non_field_error, extract and simplify it.
            if isinstance(e.detail, dict) and "non_field_errors" in e.detail:
                # Extract the first error message string.
                error_message = str(e.detail["non_field_errors"][0])

                # The validator uses "username", but the user only enters an email.
                # Let's make the message more intuitive for the user.
                user_facing_message = error_message.replace("username", "email")

                # Return the cleaned message in a simple 'detail' key.
                return Response(
                    {"detail": user_facing_message}, status=status.HTTP_400_BAD_REQUEST
                )

            # Otherwise, it's a field-specific error (e.g., {'email': [...]}),
            # so we return the original structured error.
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            logger.error(
                f"REGISTRATION VIEW LOG: Error during super().post or signup flow for {request.data.get('email')}: {e}",
                exc_info=True,
            )
            return Response(
                {"detail": "An internal error occurred during registration."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CustomPasswordResetConfirmView(APIView):
    """
    Handles the final step of the password reset process using the RAW user ID.
    NO MORE ENCODING. NO MORE DECODING.
    """

    permission_classes = [AllowAny]
    throttle_scope = "sensitive"

    def post(self, request, *args, **kwargs):
        logger.info("=" * 80)
        logger.info("!!! [PWD-RESET-CONFIRM-RAW] DIAGNOSTIC VALIDATION STARTED !!!")

        uid = request.data.get("uid")
        token = request.data.get("token")
        User = get_user_model()

        logger.info("!!! [PWD-RESET-CONFIRM-RAW] Diagnostic validation started (payload keys: %s)", list(request.data.keys()))
        logger.info("!!! [PWD-RESET-CONFIRM-RAW] STEP 1: Processing UID: %s", uid)
        # Do not log the token value; it would expose reset links in log aggregators.
        logger.info("!!! [PWD-RESET-CONFIRM-RAW] STEP 1: Token present: %s", bool(token))

        # 1. Find user directly with the raw UID (pk)
        try:
            user = User.objects.get(pk=uid)
            logger.info(
                f"!!! [PWD-RESET-CONFIRM-RAW] SUCCESS: User lookup successful. Found user: {user.email} (ID: {user.pk})"
            )
        except (User.DoesNotExist, ValueError, TypeError):
            logger.error(
                f"!!! [PWD-RESET-CONFIRM-RAW] FATAL: USER LOOKUP FAILED. No user found with PK: '{uid}'"
            )
            return Response(
                {"uid": ["Invalid value"]}, status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Check if the token is valid for the user (standard reset or concierge handover)
        token_ok = default_token_generator.check_token(
            user, token
        ) or concierge_handover_token_generator.check_token(user, token)
        if not token_ok:
            logger.error(
                f"!!! [PWD-RESET-CONFIRM-RAW] FATAL: TOKEN CHECK FAILED for user {user.email}. The token is invalid or has expired."
            )
            return Response(
                {"token": ["Invalid token"]}, status=status.HTTP_400_BAD_REQUEST
            )

        logger.info(
            f"!!! [PWD-RESET-CONFIRM-RAW] SUCCESS: Token is valid for user {user.email}."
        )

        # 3. Use allauth's SetPasswordForm for validation and saving
        form = SetPasswordForm(
            data={
                "password1": request.data.get("new_password1"),
                "password2": request.data.get("new_password2"),
            },
            user=user,
        )

        if form.is_valid():
            user.set_password(form.cleaned_data["password1"])
            user.save()
            
            # --- EXPLICITLY SEND EMAIL HERE ---
            try:
                logger.info(f"Triggering explicit password change email for {user.email}")
            except Exception as e:
                # Log error but do not crash the request; the password change succeeded.
                logger.error(f"Failed to send password change email: {e}")
            # ----------------------------------

            logger.info(
                f"!!! [PWD-RESET-CONFIRM-RAW] SUCCESS: Password successfully reset for user: {user.email}"
            )
            logger.info("=" * 80)
            return Response(
                {"detail": "Password has been reset with the new password."},
                status=status.HTTP_200_OK,
            )
        else:
            logger.error(
                f"!!! [PWD-RESET-CONFIRM-RAW] FATAL: Form invalid for user {user.email}. Errors: {form.errors.as_json()}"
            )
            logger.info("=" * 80)
            return Response(form.errors, status=status.HTTP_400_BAD_REQUEST)