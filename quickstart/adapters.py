# quickstart/adapters.py

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.account.utils import user_field
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.contrib.auth.tokens import default_token_generator
import logging

logger = logging.getLogger(__name__)


class CustomAccountAdapter(DefaultAccountAdapter):
    def get_email_confirmation_url(self, request, emailconfirmation):
        """
        Constructs the email confirmation (activation) URL.
        """
        base_url = settings.FRONTEND_BASE_URL
        path = settings.FRONTEND_EMAIL_VERIFICATION_PATH.format(
            key=emailconfirmation.key
        )
        return f"{base_url}{path}"

    def get_password_reset_url(self, request, user, temp_key):
        """
        Constructs the password reset URL for the frontend.
        THE UID IS NOW THE RAW, UNENCODED USER PK.
        """
        base_url = settings.FRONTEND_BASE_URL

        # --- THE FIX: NO MORE FUCKING ENCODING. USE THE RAW PK. ---
        uid = user.pk

        logger.info(f"CustomAccountAdapter - User PK: {user.pk}")
        logger.info(f"RAW UID being sent to frontend: {uid}")
        logger.info(f"Generated token: {temp_key}")

        path = settings.FRONTEND_PASSWORD_RESET_CONFIRM_PATH.format(
            uid=uid, token=temp_key
        )
        full_url = f"{base_url}{path}"
        logger.info(f"Complete password reset URL: {full_url}")

        return full_url

    def send_account_already_exists_mail(self, email):
        """
        By overriding with `pass`, we simply do nothing.
        """
        logger.warning(
            f"Prevented 'account already exists' email for existing user: {email}"
        )
        pass


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        if sociallogin.is_existing:
            return

        if not sociallogin.email_addresses:
            return

        verified_email = None
        for email in sociallogin.email_addresses:
            if email.verified:
                verified_email = email.email
                break

        if not verified_email:
            return

        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=verified_email)
            sociallogin.connect(request, user)
            logger.info(
                f"Auto-connected social account for {verified_email} to existing user."
            )
        except User.DoesNotExist:
            pass
        except User.MultipleObjectsReturned:
            logger.error(
                f"Multiple users found with email {verified_email}. Cannot auto-connect social account."
            )
            pass

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        first_name = data.get("first_name")
        last_name = data.get("last_name")
        if not first_name and sociallogin.account.extra_data:
            first_name = sociallogin.account.extra_data.get("given_name")
        if not last_name and sociallogin.account.extra_data:
            last_name = sociallogin.account.extra_data.get("family_name")
        if first_name:
            user_field(user, "first_name", first_name)
        if last_name:
            user_field(user, "last_name", last_name)
        logger.info(
            f"Populated user {user.email} with social data: Name='{first_name} {last_name}'"
        )
        return user

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        if not user.role:
            try:
                from .models import Role

                default_role, created = Role.objects.get_or_create(
                    name="Student",
                    defaults={
                        "is_default": True,
                        "is_system": False,
                        "hierarchy_level": 10,
                        "color": "#6c757d",
                    },
                )
                user.role = default_role
                user.save(update_fields=["role"])
                logger.info(
                    f"Assigned default 'Student' role to new social user {user.email}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to assign default role to social user {user.email}: {e}"
                )
        return user
