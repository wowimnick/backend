# quickstart/tests/test_views/test_stripe_connect_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from unittest.mock import patch, MagicMock

from quickstart.models import BusinessInfo
from quickstart.tests.factories import UserFactory, BusinessInfoFactory, RoleFactory
from quickstart.utils.permissions import CanManageOwnBusinessProfile
from django.contrib.auth.models import Permission


class StripeConnectViewTests(APITestCase):
    def setUp(self):
        # Create a user and assign them the necessary permission
        self.user = UserFactory()
        business_role = RoleFactory(name="Business Owner")
        self.user.role = business_role
        self.user.save()
        permission = Permission.objects.get(codename="manage_own_business_profile")
        self.user.user_permissions.add(permission)

        self.client.force_authenticate(user=self.user)
        self.url = reverse("stripe-connect-onboarding")

    @patch("stripe.Account.create")
    @patch("stripe.AccountLink.create")
    def test_create_stripe_account_and_get_onboarding_link(
        self, mock_account_link_create, mock_account_create
    ):
        """
        POST - A user with a new business should create a Stripe account and get a link.
        """
        # Setup Mocks
        mock_account_create.return_value = MagicMock(id="acct_new_test123")
        mock_account_link_create.return_value = MagicMock(
            url="https://connect.stripe.com/onboard/test_url"
        )

        # Create a business without a stripe_account_id
        business = BusinessInfoFactory(owner=self.user, stripe_account_id=None)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["accountLinkUrl"],
            "https://connect.stripe.com/onboard/test_url",
        )
        mock_account_create.assert_called_once()
        mock_account_link_create.assert_called_once()

        # Verify the business was updated in the DB
        business.refresh_from_db()
        self.assertEqual(business.stripe_account_id, "acct_new_test123")

    @patch("stripe.Account.create")
    @patch("stripe.AccountLink.create")
    def test_get_onboarding_link_for_existing_account(
        self, mock_account_link_create, mock_account_create
    ):
        """
        POST - A user with an existing Stripe account ID should get a new link without creating an account.
        """
        mock_account_link_create.return_value = MagicMock(
            url="https://connect.stripe.com/onboard/existing_acct_url"
        )

        # Business already has a stripe_account_id
        BusinessInfoFactory(owner=self.user, stripe_account_id="acct_existing_456")

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["accountLinkUrl"],
            "https://connect.stripe.com/onboard/existing_acct_url",
        )
        # The key assertion: Account.create should NOT be called
        mock_account_create.assert_not_called()
        mock_account_link_create.assert_called_once()
        self.assertEqual(
            mock_account_link_create.call_args.kwargs["account"], "acct_existing_456"
        )

    def test_get_status_for_unlinked_account(self):
        """
        GET - Should return 'unlinked' status for a business with no Stripe ID.
        """
        BusinessInfoFactory(owner=self.user, stripe_account_id=None)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "unlinked")
        self.assertFalse(response.data["is_onboarding_complete"])

    @patch("stripe.Account.retrieve")
    def test_get_status_for_active_account(self, mock_account_retrieve):
        """
        GET - Should correctly identify and return 'active' status.
        """
        # Mock a fully onboarded Stripe account
        mock_account_retrieve.return_value = MagicMock(
            id="acct_active_789",
            charges_enabled=True,
            payouts_enabled=True,
            details_submitted=True,
            requirements={  # Using a real dict to avoid serialization errors
                "currently_due": [],
                "eventually_due": [],
                "pending_verification": [],
            },
            # THIS IS THE FIX: Explicitly set disabled_reason to None
            disabled_reason=None,
            type="express",
        )
        business = BusinessInfoFactory(
            owner=self.user, stripe_account_id="acct_active_789"
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "active")
        self.assertTrue(response.data["is_onboarding_complete"])

        # Check if the DB was updated (if it was different before)
        business.refresh_from_db()
        self.assertEqual(business.stripe_account_status, "active")

    @patch("stripe.Account.retrieve")
    def test_get_status_for_restricted_account(self, mock_account_retrieve):
        """
        GET - Should correctly identify and return 'restricted' status.
        """
        # Mock an account that is restricted
        mock_account_retrieve.return_value = MagicMock(
            id="acct_restricted_101",
            charges_enabled=False,
            payouts_enabled=False,
            details_submitted=True,
            requirements={  # Using a real dict
                "currently_due": ["individual.verification.document"],
                "eventually_due": [],
                "pending_verification": [],
            },
            disabled_reason="requirements.past_due",
            type="express",
        )
        BusinessInfoFactory(owner=self.user, stripe_account_id="acct_restricted_101")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "restricted")
        self.assertFalse(response.data["is_onboarding_complete"])

    def test_user_without_business_cannot_access_stripe_connect(self):
        """
        POST/GET - A user without a business profile should be denied access.
        """
        # The setUp creates a user but no business. Let's create a new user to be sure.
        user_no_business = UserFactory()
        self.client.force_authenticate(user=user_no_business)

        # Test POST
        post_response = self.client.post(self.url)
        self.assertEqual(post_response.status_code, status.HTTP_404_NOT_FOUND)

        # Test GET
        get_response = self.client.get(self.url)
        self.assertEqual(get_response.status_code, status.HTTP_404_NOT_FOUND)
