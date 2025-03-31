from django.conf import settings
from rest_framework_simplejwt.tokens import AccessToken
import jwt

class JWTCookieMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Try to get JWT from cookie first
        access_token = request.COOKIES.get(settings.SIMPLE_JWT['AUTH_COOKIE'])
        
        # If token exists in cookie, add it to the Authorization header
        if access_token:
            request.META['HTTP_AUTHORIZATION'] = f'Bearer {access_token}'
            
            # For non-GET, non-HEAD requests, ensure CSRF token is validated
            if request.method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
                # Django's CSRF middleware will handle the validation
                # Just ensure we're not bypassing it
                pass

        response = self.get_response(request)
        return response