# quickstart/views/social_auth_views.py
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.views import SocialLoginView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from django.contrib.auth import get_user_model
import logging

from quickstart.utils.login_audit import log_user_login

# --- MODIFIED: Import the necessary decorators ---
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


# This view is designed to be used with a token-based flow from the frontend.
# E.g., @react-oauth/google sends an access_token.


# --- MODIFIED: Apply the csrf_exempt decorator to the class ---
@method_decorator(csrf_exempt, name="dispatch")
class GoogleLogin(SocialLoginView):
    adapter_class = GoogleOAuth2Adapter
    client_class = OAuth2Client
    # The callback_url is not necessary for this token-based flow.

    def post(self, request, *args, **kwargs):
        """
        Overrides the default post method to align the response format
        and authentication mechanism (HttpOnly cookies) with the rest of the application.
        """
        # The parent post method from SocialLoginView will perform the login
        # or registration and return tokens in the response body.
        try:
            # This calls allauth, creates/logs in user, and gets JWTs from simple-jwt
            response = super().post(request, *args, **kwargs)
        except Exception as e:
            logger.error(
                f"Error during Google social login processing: {e}", exc_info=True
            )
            return Response(
                {"detail": "An error occurred while trying to log in with Google."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if response.status_code == 200:
            # Login/registration was successful. Now, format the response.
            access_token = response.data.get("access")
            refresh_token = response.data.get("refresh")
            user_data = response.data.get("user")

            if not all([access_token, refresh_token, user_data]):
                logger.error(
                    f"Google login succeeded but response from dj-rest-auth is missing tokens/user. Response: {response.data}"
                )
                return Response(
                    {
                        "detail": "Internal server error during authentication handshake."
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # The final response should only contain the user data in the body.
            # Tokens will be in HttpOnly cookies.
            final_response = Response({"user": user_data}, status=status.HTTP_200_OK)

            # Set HttpOnly cookies for security
            final_response.set_cookie(
                key=settings.SIMPLE_JWT["AUTH_COOKIE"],
                value=access_token,
                max_age=settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds(),
                httponly=True,
                samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
                secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
            )
            final_response.set_cookie(
                key=settings.SIMPLE_JWT["AUTH_COOKIE_REFRESH"],
                value=refresh_token,
                max_age=settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds(),
                httponly=True,
                samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
                secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
            )

            user = None
            user_id = user_data.get("userId")
            if user_id is not None:
                user = get_user_model().objects.filter(pk=user_id).first()
            if user is None and user_data.get("email"):
                user = get_user_model().objects.filter(
                    email__iexact=user_data["email"]
                ).first()
            if user:
                log_user_login(user, request)

            logger.info(
                f"Successfully processed and served Google login for user: {user_data.get('email')}"
            )
            return final_response
        else:
            # If the parent call failed (e.g., invalid token from frontend), pass its error response through.
            logger.warning(
                f"Google login failed with status {response.status_code}. Response: {response.data}"
            )
            return response
