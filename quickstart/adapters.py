# quickstart/adapters.py

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.account.utils import user_field
from django.contrib.auth import get_user_model  # <-- IMPORT THIS
import logging

logger = logging.getLogger(__name__)


# THIS IS THE NEW PART
class CustomAccountAdapter(DefaultAccountAdapter):
    def send_account_already_exists_mail(self, email):
        """
        This method is called when a user tries to sign up with an email
        that already exists. In a headless setup, we don't want to send
        an email with a link to a non-existent signup page.

        By overriding it with `pass`, we simply do nothing.
        """
        logger.warning(
            f"Prevented 'account already exists' email for existing user: {email}"
        )
        pass


# This is your existing Social Account Adapter - it remains the same.
class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        """
        Invoked just after a user successfully authenticates via a
        social provider, but before the login is actually processed
        (and before any local user account is created).

        We're overriding this to automatically link social accounts to
        existing user accounts based on matching, verified email addresses.
        This avoids the "account already exists" error when a user who
        has already signed up with an email and password tries to log in
        with a social account using the same email.
        """
        # Ignore if user is already logged in and connecting a new account
        if sociallogin.is_existing:
            return

        # Check for a verified email from the social provider
        if not sociallogin.email_addresses:
            return

        verified_email = None
        for email in sociallogin.email_addresses:
            if email.verified:
                verified_email = email.email
                break

        if not verified_email:
            return

        # Get the User model correctly
        User = get_user_model()  # <-- GET THE MODEL HERE

        # Check if a user with this email already exists in our system
        try:
            # Use the User model variable
            user = User.objects.get(email__iexact=verified_email)

            # If the user exists, connect this new social login to their account
            sociallogin.connect(request, user)
            logger.info(
                f"Auto-connected social account for {verified_email} to existing user."
            )

        except User.DoesNotExist:  # <-- CATCH THE CORRECT EXCEPTION
            # If the user does not exist, let the normal signup flow continue
            pass
        except User.MultipleObjectsReturned:  # <-- CATCH THE CORRECT EXCEPTION
            logger.error(
                f"Multiple users found with email {verified_email}. Cannot auto-connect social account."
            )
            # You might want to raise a specific error here to be caught by the view
            pass

    def populate_user(self, request, sociallogin, data):
        """
        Populates user fields from social account data.
        """
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
        """
        Saves a new user instance, and runs custom logic after the first save.
        """
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
