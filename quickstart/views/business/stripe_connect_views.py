# quickstart/views/business/stripe_connect_views.py

import stripe
from django.conf import settings
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound
from rest_framework.throttling import ScopedRateThrottle
from django.db.models import Q

from quickstart.models import BusinessInfo  # Ensure this path is correct
from quickstart.utils.permissions import (
    CanManageOwnBusinessProfile,
)  # Ensure this path is correct

import logging

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


class StripeConnectView(views.APIView):
    permission_classes = [IsAuthenticated, CanManageOwnBusinessProfile]
    # --- Rate Limiting ---
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sensitive"

    def get_business_object(self, request):
        user = request.user
        business = (
            BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user))
            .distinct()
            .first()
        )
        if not business:
            raise NotFound(
                "No business profile associated with this user or permission denied."
            )
        return business

    def post(self, request, *args, **kwargs):
        try:
            business = self.get_business_object(request)
        except NotFound:
            return Response(
                {"error": "Business profile not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        stripe_account_id = business.stripe_account_id

        frontend_domain = getattr(
            settings, "FRONTEND_BASE_URL", "http://localhost:3000"
        ).rstrip("/")
        business_page_on_platform = f"{frontend_domain}/b/{business.businessId}"
        profile_url_for_stripe = None

        if (
            business.website
            and not ("localhost" in business.website or "127.0.0.1" in business.website)
            and business.website.startswith(("http://", "https://"))
        ):
            profile_url_for_stripe = business.website
            logger.info(
                f"StripeConnect: Using business.website for Stripe business_profile.url: {profile_url_for_stripe}"
            )

        if not profile_url_for_stripe:
            if not (
                "localhost" in business_page_on_platform
                or "127.0.0.1" in business_page_on_platform
            ) and business_page_on_platform.startswith(("http://", "https://")):
                profile_url_for_stripe = business_page_on_platform
                logger.info(
                    f"StripeConnect: Using platform-generated page for Stripe business_profile.url: {profile_url_for_stripe}"
                )

        if not profile_url_for_stripe:
            profile_url_for_stripe = f"https://www.classeasily.com/stripe-profile-placeholder/b/{business.businessId}"
            logger.warning(
                f"StripeConnect: Business {business.businessId} - business.website and platform URL were localhost or unsuitable. "
                f"Using generic placeholder for Stripe business_profile.url: {profile_url_for_stripe}"
            )

        if not profile_url_for_stripe.startswith("https://www.classeasily.com"):
            if profile_url_for_stripe.startswith("http://"):
                if settings.DEBUG:
                    logger.warning(
                        f"StripeConnect: business_profile.url for Business {business.businessId} is HTTP in DEBUG: "
                        f"{profile_url_for_stripe}. Stripe prefers HTTPS."
                    )
                else:
                    logger.error(
                        f"StripeConnect: business_profile.url for Business {business.businessId} is HTTP ({profile_url_for_stripe}) "
                        f"in a non-DEBUG environment. This should ideally be HTTPS. Attempting to upgrade."
                    )
                    profile_url_for_stripe = profile_url_for_stripe.replace(
                        "http://", "https://", 1
                    )
                    logger.info(
                        f"StripeConnect: Upgraded business_profile.url to HTTPS: {profile_url_for_stripe}"
                    )

        # Define placeholder for account_id if it's not yet created for URL construction
        temp_account_id_for_url = (
            stripe_account_id if stripe_account_id else "new_account_placeholder"
        )

        refresh_url_path = f"/stripe-connect/return?stripe_refresh=true&original_intent=dashboard_settings&account_id={temp_account_id_for_url}"
        return_url_path = f"/stripe-connect/return?stripe_return=true&original_intent=dashboard_settings&account_id={temp_account_id_for_url}"

        full_refresh_url = f"{frontend_domain}{refresh_url_path}"
        full_return_url = f"{frontend_domain}{return_url_path}"

        try:
            if not stripe_account_id:
                account_params = {
                    "type": "express",
                    "country": getattr(
                        business, "businessCountry", "CA"
                    ),  # Example: business.businessCountry or default
                    "email": business.studentContactEmail or business.owner.email,
                    "capabilities": {
                        "card_payments": {"requested": True},
                        "transfers": {"requested": True},
                    },
                    "business_profile": {
                        "name": business.businessName,
                        "url": profile_url_for_stripe,
                    },
                    "metadata": {
                        "classeasily_business_id": str(business.businessId),
                        "classeasily_owner_email": str(business.owner.email),
                    },
                }

                if (
                    business.businessType
                    and business.businessType.lower() == "individual"
                ):
                    owner = business.owner
                    account_params["business_type"] = "individual"
                    account_params["individual"] = {
                        "first_name": owner.first_name,
                        "last_name": owner.last_name,
                        "email": owner.email,
                    }
                else:
                    account_params["business_type"] = "company"
                    account_params["company"] = {"name": business.businessName}

                logger.info(
                    f"StripeConnect: Attempting to create Stripe Express account with params: {account_params}"
                )
                stripe_account = stripe.Account.create(**account_params)
                stripe_account_id = stripe_account.id
                business.stripe_account_id = stripe_account_id
                business.stripe_account_status = "incomplete"
                business.save(
                    update_fields=["stripe_account_id", "stripe_account_status"]
                )
                logger.info(
                    f"StripeConnect: Created Stripe Express account {stripe_account_id} for Business {business.businessId}"
                )

                # Update AccountLink URLs with the new ID
                refresh_url_path = f"/stripe-connect/return?stripe_refresh=true&original_intent=dashboard_settings&account_id={stripe_account_id}"
                return_url_path = f"/stripe-connect/return?stripe_return=true&original_intent=dashboard_settings&account_id={stripe_account_id}"
                full_refresh_url = f"{frontend_domain}{refresh_url_path}"
                full_return_url = f"{frontend_domain}{return_url_path}"

            account_link_params = {
                "account": stripe_account_id,
                "refresh_url": full_refresh_url,
                "return_url": full_return_url,
                "type": "account_onboarding",
            }
            logger.info(
                f"StripeConnect: Creating Account Link with params: {account_link_params}"
            )
            account_link = stripe.AccountLink.create(**account_link_params)

            logger.info(
                f"StripeConnect: Created Account Link for Stripe account {stripe_account_id}. Return URL: {full_return_url}, Refresh URL: {full_refresh_url}"
            )
            return Response(
                {"accountLinkUrl": account_link.url}, status=status.HTTP_200_OK
            )

        except stripe.StripeError as e:
            user_message = getattr(
                e,
                "user_message",
                "We couldn't connect to Stripe. Please check your details and try again.",
            )
            error_param = getattr(e, "param", "N/A")
            logger.error(
                f"StripeConnect: Stripe API error for Business {business.businessId} (Account: {stripe_account_id}). "
                f"Param: {error_param}, User Message: '{user_message}'",
                exc_info=True,
            )
            return Response({"error": user_message}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"StripeConnect: Non-Stripe error for Business {business.businessId} (Account: {stripe_account_id}): {str(e)}",
                exc_info=True,
            )
            return Response(
                {
                    "error": "An unexpected server error occurred. Our team has been notified. Please try again later."
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def get(self, request, *args, **kwargs):
        try:
            business = self.get_business_object(request)
        except NotFound:
            return Response(
                {"error": "Business profile not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not business.stripe_account_id:
            return Response(
                {
                    "status": "unlinked",
                    "details_submitted": False,
                    "payouts_enabled": False,
                    "charges_enabled": False,
                    "is_onboarding_complete": False,
                }
            )

        logger.info(
            f"--- GET Endpoint: Evaluating status for Stripe Account: {business.stripe_account_id} ---"
        )

        try:
            account = stripe.Account.retrieve(business.stripe_account_id)

            logger.info(
                f"GET Endpoint: Current DB status is '{business.stripe_account_status}'."
            )

            # --- DETAILED LOGGING OF INCOMING STRIPE DATA ---
            requirements = account.get("requirements", {})
            disabled_reason = account.get("disabled_reason")

            currently_due = requirements.get("currently_due", [])
            eventually_due = requirements.get("eventually_due", [])
            pending_verification = requirements.get("pending_verification", [])

            logger.info(
                f"GET Endpoint: Stripe Data | charges_enabled: {account.charges_enabled}"
            )
            logger.info(
                f"GET Endpoint: Stripe Data | payouts_enabled: {account.payouts_enabled}"
            )
            logger.info(
                f"GET Endpoint: Stripe Data | details_submitted: {account.details_submitted}"
            )
            logger.info(
                f"GET Endpoint: Stripe Data | disabled_reason: '{disabled_reason}'"
            )
            logger.info(f"GET Endpoint: Stripe Data | currently_due: {currently_due}")
            logger.info(
                f"GET Endpoint: Stripe Data | pending_verification: {pending_verification}"
            )
            logger.info(f"GET Endpoint: Stripe Data | eventually_due: {eventually_due}")
            # --- END OF DETAILED LOGGING ---

            new_platform_status = "unlinked"
            is_onboarding_complete = False

            if (
                account.charges_enabled
                and account.payouts_enabled
                and not currently_due
                and not eventually_due
                and not disabled_reason
            ):
                new_platform_status = "active"
                is_onboarding_complete = True
            elif disabled_reason or currently_due:
                new_platform_status = "restricted"
            elif pending_verification:
                new_platform_status = "pending"
            elif not account.details_submitted:
                new_platform_status = "incomplete"
            else:
                new_platform_status = "pending"

            logger.info(
                f"GET Endpoint: Final determined status is '{new_platform_status}'."
            )

            if business.stripe_account_status != new_platform_status:
                business.stripe_account_status = new_platform_status
                business.save(update_fields=["stripe_account_status"])
                logger.info(
                    f"GET Endpoint: SUCCESS! Business {business.businessId} status updated from '{business.stripe_account_status}' to '{new_platform_status}'."
                )
            else:
                logger.info(
                    f"GET Endpoint: Status '{new_platform_status}' is unchanged. No DB update needed."
                )

            return Response(
                {
                    "stripe_account_id": account.id,
                    "status": new_platform_status,
                    "raw_stripe_status": {
                        "charges_enabled": account.charges_enabled,
                        "details_submitted": account.details_submitted,
                        "payouts_enabled": account.payouts_enabled,
                        "type": account.type,
                    },
                    "requirements": dict(account.requirements),
                    "is_onboarding_complete": is_onboarding_complete,
                }
            )
        except stripe.StripeError as e:
            logger.error(
                f"GET Endpoint: Stripe API error retrieving account {business.stripe_account_id}: {str(e)}"
            )
            return Response(
                {
                    "stripe_account_id": business.stripe_account_id,
                    "status": business.stripe_account_status or "unlinked",
                    "error": "Could not fetch latest status from Stripe. Please try refreshing.",
                    "is_onboarding_complete": False,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.error(
                f"GET Endpoint: Unexpected error retrieving Stripe account {business.stripe_account_id}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {
                    "error": "An unexpected error occurred while fetching payment status."
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
