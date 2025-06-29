from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from dj_rest_auth.registration.views import RegisterView
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth import get_user_model
from django.conf import settings
import logging

from django.views import View
from django.http import JsonResponse

from quickstart.models import AuditLog


from quickstart.serializers import (
    CustomTokenObtainPairSerializer,
    CustomUserDetailsSerializer,
    CustomRegisterSerializer,
)

logger = logging.getLogger(__name__)


def get_client_ip(request):
    """Get client IP address from request."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0]
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip


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
            # You could add a 'failed_login' audit log here if desired
            # AuditLog.objects.create(...)
            error_detail = e.args[0] if e.args else "Invalid credentials."
            return Response(
                {"detail": error_detail}, status=status.HTTP_401_UNAUTHORIZED
            )

        # --- LOGIN IS SUCCESSFUL AT THIS POINT ---

        validated_data = serializer.validated_data
        user = serializer.user  # The serializer conveniently gives us the user object

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
            # Log the error but don't fail the login process
            logger.error(
                f"Failed to create login audit log for user {user.email}: {audit_error}"
            )

        # --- 2. ASSEMBLE RESPONSE PAYLOAD ---
        response_data = {
            "user": validated_data["user"],
        }
        response = Response(response_data, status=status.HTTP_200_OK)

        # --- 3. SET COOKIES ---
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
        refresh_token = request.COOKIES.get(settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"])

        if not refresh_token:
            return Response(
                {"detail": "Refresh token not found in cookies"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        User = get_user_model()

        try:
            refresh = RefreshToken(refresh_token)

            # --- Get User and Permissions ---
            user_id = refresh.payload.get("user_id")
            # The User variable is already defined and available
            try:
                user = User.objects.select_related("role").get(userId=user_id)
                user_serializer = CustomUserDetailsSerializer(user)
                user_data = user_serializer.data
            except User.DoesNotExist:
                logger.error(
                    f"User with ID {user_id} from valid refresh token not found."
                )
                raise TokenError("User not found.")
            # ----------------------------

            data = {"access": str(refresh.access_token), "user": user_data}

            if settings.SIMPLE_JWT["ROTATE_REFRESH_TOKENS"]:
                new_refresh = RefreshToken.for_user(user)
                data["refresh"] = str(new_refresh)

            response = Response(data, status=status.HTTP_200_OK)

            response.set_cookie(
                settings.SIMPLE_JWT["AUTH_COOKIE"],
                data["access"],
                max_age=settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds(),
                httponly=True,
                samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
                secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
            )
            if settings.SIMPLE_JWT["ROTATE_REFRESH_TOKENS"]:
                response.set_cookie(
                    settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"],
                    data["refresh"],
                    max_age=settings.SIMPLE_JWT[
                        "REFRESH_TOKEN_LIFETIME"
                    ].total_seconds(),
                    httponly=True,
                    samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
                    secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
                )
                if settings.SIMPLE_JWT["BLACKLIST_AFTER_ROTATION"]:
                    try:
                        refresh.blacklist()
                    except AttributeError:
                        pass

            return response

        except (TokenError, AttributeError, User.DoesNotExist) as e:
            logger.error(f"Token Refresh Error: {e}")
            # Ensure cookies are cleared on refresh failure as well
            response = Response(
                {"detail": "Token refresh failed or user not found."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE"])
            response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"])
            return response


class UserUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        print(
            f"UserUpdateView: Authenticated user: {request.user.email if request.user.is_authenticated else 'Anonymous'}"
        )  # Debug log
        print("Received data:", request.data)

        serializer = CustomUserDetailsSerializer(
            request.user,  # This should now be the authenticated user instance
            data=request.data,
            partial=True,
            context={"request": request},  # Good practice to pass request context
        )
        if serializer.is_valid():
            print("Valid data:", serializer.validated_data)
            serializer.save()
            return Response(serializer.data)
        print("Serializer errors:", serializer.errors)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LogoutView(APIView):
    def post(self, request):
        try:
            refresh_token = request.COOKIES.get(
                settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"]
            )

            response = Response(status=status.HTTP_205_RESET_CONTENT)

            # Always delete cookies, even if token processing fails
            response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE"])
            response.delete_cookie(settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"])
            # Also delete the CSRF token to ensure a clean state for the next session.
            response.delete_cookie(settings.CSRF_COOKIE_NAME)

            # Optional: Attempt to blacklist token if present
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
            # Call the parent method which handles user creation and triggers signup completion
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

        except Exception as e:
            # Catch any unexpected errors during the super().post call or user creation
            logger.error(
                f"REGISTRATION VIEW LOG: Error during super().post or signup flow for {request.data.get('email')}: {e}",
                exc_info=True,
            )
            # Return a generic error response
            return Response(
                {"detail": "An internal error occurred during registration."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
