# quickstart/tests/test_views/test_support_ticket_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from unittest.mock import patch
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import SupportTicket, Role, ChatMessage, ChatSession
from quickstart.tests.factories import UserFactory, RoleFactory


def _get_and_assign_permission(role, perm_codename, model_class):
    """Helper to get or create a permission and assign it to a role."""
    content_type = ContentType.objects.get_for_model(model_class)
    permission, _ = Permission.objects.get_or_create(
        codename=perm_codename,
        content_type=content_type,
    )
    role.permissions.add(permission)


class SupportTicketSystemTests(APITestCase):
    """
    Tests for the user-facing support ticket system.
    """

    def setUp(self):
        # Create Role and Permissions
        user_role = RoleFactory(name="Standard User", is_default=True)
        _get_and_assign_permission(user_role, "add_supportticket", SupportTicket)
        _get_and_assign_permission(user_role, "reply_own_support_ticket", SupportTicket)

        # Create Users
        self.user1 = UserFactory(role=user_role)
        self.user2 = UserFactory(role=user_role)

        # Assign permissions directly to users for test client
        self.user1.user_permissions.add(*user_role.permissions.all())
        self.user2.user_permissions.add(*user_role.permissions.all())

    # FIX: Corrected the patch path to point where the function is *called from*.
    @patch(
        "quickstart.views.public.support_ticket_views.send_support_ticket_created_email"
    )
    def test_user_can_create_support_ticket(self, mock_send_email):
        """
        POST /api/support-tickets/ - A logged-in user can create a support ticket.
        """
        print("\n--- Running: test_user_can_create_support_ticket ---")
        self.client.force_authenticate(user=self.user1)
        url = reverse("user-support-ticket-list")
        data = {
            "subject": "Issue with my booking",
            "description": "I can't seem to find the cancellation button for my upcoming class.",
            "category": "booking",
            "priority": "medium",
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(SupportTicket.objects.filter(user=self.user1).exists())
        ticket = SupportTicket.objects.get(user=self.user1)
        self.assertEqual(ticket.subject, data["subject"])
        self.assertTrue(ticket.chat_session.messages.filter(is_user=True).exists())
        self.assertEqual(
            ticket.chat_session.messages.first().content, data["description"]
        )
        mock_send_email.assert_called_once()
        print("✅ PASSED: User can successfully create a support ticket.")

    def test_creating_ticket_with_empty_subject_fails(self):
        """
        [EDGE CASE] POST /api/support-tickets/ - Fails when subject is empty.
        """
        print("\n--- Running: test_creating_ticket_with_empty_subject_fails ---")
        self.client.force_authenticate(user=self.user1)
        url = reverse("user-support-ticket-list")
        data = {
            "subject": "",
            "description": "Valid description.",
            "category": "technical",
            "priority": "low",
        }

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subject", response.data)
        print("✅ PASSED: Ticket creation failed with empty subject as expected.")

    def test_user_can_reply_to_own_ticket(self):
        """
        POST /api/support-tickets/{pk}/reply/ - The ticket owner can add a reply.
        """
        print("\n--- Running: test_user_can_reply_to_own_ticket ---")

        # FIX: Create the ticket and its associated ChatSession, mimicking the real app flow.
        session = ChatSession.objects.create(userId=self.user1)
        ticket = SupportTicket.objects.create(
            user=self.user1,
            subject="Initial problem",
            description="Initial description",
            category="technical",
            chat_session=session,  # Link the session to the ticket
        )

        self.client.force_authenticate(user=self.user1)
        url = reverse("user-support-ticket-reply", kwargs={"pk": ticket.pk})
        data = {"message": "I forgot to mention, I am using the Chrome browser."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            ChatMessage.objects.filter(
                session=ticket.chat_session, content=data["message"]
            ).exists()
        )
        print("✅ PASSED: User can successfully reply to their own ticket.")

    def test_user_cannot_reply_to_closed_ticket(self):
        """
        [EDGE CASE] POST .../reply/ - A user cannot reply to a ticket that is closed.
        """
        print("\n--- Running: test_user_cannot_reply_to_closed_ticket ---")
        session = ChatSession.objects.create(userId=self.user1)
        ticket = SupportTicket.objects.create(
            user=self.user1,
            subject="Closed problem",
            status="closed",
            chat_session=session,
        )

        self.client.force_authenticate(user=self.user1)
        url = reverse("user-support-ticket-reply", kwargs={"pk": ticket.pk})
        data = {"message": "Trying to reply to a closed ticket."}

        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cannot reply to a closed ticket", response.data["error"])
        print("✅ PASSED: User blocked from replying to a closed ticket.")

    def test_user_cannot_view_another_users_ticket(self):
        """
        GET /api/support-tickets/{pk}/ - A user cannot retrieve another user's ticket.
        """
        print("\n--- Running: test_user_cannot_view_another_users_ticket ---")
        ticket_user1 = SupportTicket.objects.create(
            user=self.user1,
            subject="User 1's secret ticket",
            description="...",
            category="other",
        )

        self.client.force_authenticate(user=self.user2)
        url = reverse("user-support-ticket-detail", kwargs={"pk": ticket_user1.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        print(
            "✅ PASSED: User correctly gets 404 when trying to view another's ticket."
        )

    def test_user_cannot_reply_to_another_users_ticket(self):
        """
        POST /api/support-tickets/{pk}/reply/ - A user cannot reply to another user's ticket.
        """
        print("\n--- Running: test_user_cannot_reply_to_another_users_ticket ---")
        ticket_user1 = SupportTicket.objects.create(
            user=self.user1,
            subject="User 1's secret ticket",
            description="...",
            category="other",
        )

        self.client.force_authenticate(user=self.user2)
        url = reverse("user-support-ticket-reply", kwargs={"pk": ticket_user1.pk})
        data = {"message": "I am trying to reply to a ticket that is not mine."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        print(
            "✅ PASSED: User correctly gets 404 when trying to reply to another's ticket."
        )
