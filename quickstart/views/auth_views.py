from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from dj_rest_auth.registration.views import RegisterView
from django.conf import settings
import logging

from ..serializers import (
    CustomTokenObtainPairSerializer,
    CustomUserDetailsSerializer,
    CustomRegisterSerializer
)

logger = logging.getLogger(__name__)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)

        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            raise InvalidToken(e.args[0])

        data = serializer.validated_data

        # Create response with user data
        response = Response({
            'user': data['user'],
            'role': data['role']
        }, status=status.HTTP_200_OK)

        # Set cookies
        response.set_cookie(
            settings.SIMPLE_JWT['AUTH_COOKIE'],
            data['access'],
            max_age=settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
            secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE']
        )
        
        response.set_cookie(
            settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'],
            data['refresh'],
            max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
            secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE']
        )

        return response

class CustomTokenRefreshView(APIView):
    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get(settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'])
        
        if not refresh_token:
            return Response(
                {"detail": "Refresh token not found in cookies"},
                status=status.HTTP_401_UNAUTHORIZED
            )

        try:
            refresh = RefreshToken(refresh_token)
            data = {
                'access': str(refresh.access_token),
            }

            if settings.SIMPLE_JWT['ROTATE_REFRESH_TOKENS']:
                new_refresh = RefreshToken.for_user(refresh.user)
                data['refresh'] = str(new_refresh)

            response = Response(data, status=status.HTTP_200_OK)

            # Set new access token cookie
            response.set_cookie(
                settings.SIMPLE_JWT['AUTH_COOKIE'],
                data['access'],
                max_age=settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds(),
                httponly=True,
                samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
                secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE']
            )

            # If rotating tokens, set new refresh token cookie
            if settings.SIMPLE_JWT['ROTATE_REFRESH_TOKENS']:
                response.set_cookie(
                    settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'],
                    data['refresh'],
                    max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
                    httponly=True,
                    samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
                    secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE']
                )

                if settings.SIMPLE_JWT['BLACKLIST_AFTER_ROTATION']:
                    try:
                        refresh.blacklist()
                    except AttributeError:
                        pass

            return response

        except (TokenError, AttributeError) as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_401_UNAUTHORIZED
            )

class UserUpdateView(APIView):
    def patch(self, request):
        print("Received data:", request.data)  # Add this for debugging
        serializer = CustomUserDetailsSerializer(
            request.user,
            data=request.data,
            partial=True
        )
        if serializer.is_valid():
            print("Valid data:", serializer.validated_data)  # Add this for debugging
            serializer.save()
            return Response(serializer.data)
        print("Serializer errors:", serializer.errors)  # Add this for debugging
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class LogoutView(APIView):
    def post(self, request):
        try:
            refresh_token = request.COOKIES.get(settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'])
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
            
            response = Response(status=status.HTTP_205_RESET_CONTENT)
            
            # Delete both cookies
            response.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
            response.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'])
            
            return response
            
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class CustomLoginView(APIView):
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data)
        
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            raise InvalidToken(e.args[0])

        data = serializer.validated_data

        response = Response({
            'user': data['user'],
            'role': data['role']
        }, status=status.HTTP_200_OK)

        response.set_cookie(
            settings.SIMPLE_JWT['AUTH_COOKIE'],
            data['access'],
            max_age=settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
            secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE']
        )
        
        response.set_cookie(
            settings.SIMPLE_JWT['AUTH_COOKIE_REFRESH'],
            data['refresh'],
            max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
            httponly=True,
            samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE'],
            secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE']
        )

        return response

class CustomRegisterView(RegisterView):
    serializer_class = CustomRegisterSerializer

    def post(self, request, *args, **kwargs):
        logger.debug(f"Registration request data: {request.data}")
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            logger.error(f"Serializer errors: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        return super().post(request, *args, **kwargs)