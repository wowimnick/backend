from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import SupportTicket, Role, TicketMessage, CustomUser
from quickstart.tests.factories import UserFactory, RoleFactory, SupportTicketFactory


def _get_and_assign_permission(role, perm_codename, model_class):
    """Helper to get or create a permission and assign it to a role."""
    content_type = ContentType.objects.get_for_model(model_class)
    permission, _ = Permission.objects.get_or_create(
        codename=perm_codename,
        content_type=content_type,
    )
    role.permissions.add(permission)


class UserSupportTicketSystemTests(APITestCase):
    """
    Tests for the user-facing support ticket system.
    """

    def setUp(self):
        # Create Role and Permissions
        user_role = RoleFactory(name="Standard User", is_default=True)
        _get_and_assign_permission(user_role, "add_supportticket", CustomUser)
        _get_and_assign_permission(user_role, "reply_own_support_ticket", CustomUser)

        # Create Users
        self.user1 = UserFactory(role=user_role)
        self.user2 = UserFactory(role=user_role)

        # Assign permissions directly to users for test client
        self.user1.user_permissions.add(*user_role.permissions.all())
        self.user2.user_permissions.add(*user_role.permissions.all())

    def test_user_can_create_support_ticket(self):
        """
        POST /api/support-tickets/ - A logged-in user can create a support ticket.
        """
        print("\n--- Running: test_user_can_create_support_ticket ---")
        self.client.force_authenticate(user=self.user1)
        # FIX: Corrected URL name based on basename='support-ticket'
        url = reverse("support-ticket-list")
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
        self.assertTrue(ticket.conversation.filter(sender=self.user1).exists())
        self.assertEqual(ticket.conversation.first().text, data["description"])
        print("✅ PASSED: User can successfully create a support ticket.")

    def test_user_can_reply_to_own_ticket(self):
        """
        POST /api/support-tickets/{pk}/reply/ - The ticket owner can add a reply.
        """
        print("\n--- Running: test_user_can_reply_to_own_ticket ---")
        ticket = SupportTicketFactory(user=self.user1)
        self.client.force_authenticate(user=self.user1)
        # FIX: Corrected URL name based on basename='support-ticket'
        url = reverse("support-ticket-reply", kwargs={"pk": ticket.pk})
        data = {"message": "I forgot to mention, I am using the Chrome browser."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            TicketMessage.objects.filter(ticket=ticket, text=data["message"]).exists()
        )
        print("✅ PASSED: User can successfully reply to their own ticket.")

    def test_user_cannot_reply_to_another_users_ticket(self):
        """
        POST /api/support-tickets/{pk}/reply/ - A user cannot reply to another user's ticket.
        """
        print("\n--- Running: test_user_cannot_reply_to_another_users_ticket ---")
        ticket_user1 = SupportTicketFactory(user=self.user1)

        self.client.force_authenticate(user=self.user2)
        # FIX: Corrected URL name based on basename='support-ticket'
        url = reverse("support-ticket-reply", kwargs={"pk": ticket_user1.pk})
        data = {"message": "I am trying to reply to a ticket that is not mine."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        print(
            "✅ PASSED: User correctly gets 404 when trying to reply to another's ticket."
        )

    def test_user_cannot_view_another_users_ticket(self):
        """
        GET /api/support-tickets/{pk}/ - A user cannot retrieve another user's ticket.
        """
        print("\n--- Running: test_user_cannot_view_another_users_ticket ---")
        ticket_user1 = SupportTicketFactory(user=self.user1)

        self.client.force_authenticate(user=self.user2)
        # FIX: Corrected URL name based on basename='support-ticket'
        url = reverse("support-ticket-detail", kwargs={"pk": ticket_user1.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        print(
            "✅ PASSED: User correctly gets 404 when trying to view another's ticket."
        )
