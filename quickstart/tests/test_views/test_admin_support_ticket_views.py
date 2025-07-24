from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import (
    SupportTicket,
    TicketMessage,
    TicketHistoryLog,
    Role,
    CustomUser,
)
from quickstart.tests.factories import UserFactory, RoleFactory, SupportTicketFactory


def _get_and_assign_permissions(role, permissions_map):
    """Helper to assign multiple permissions to a role."""
    for model_class, codenames in permissions_map.items():
        content_type = ContentType.objects.get_for_model(model_class)
        for codename in codenames:
            permission, _ = Permission.objects.get_or_create(
                codename=codename,
                content_type=content_type,
            )
            role.permissions.add(permission)


class AdminSupportTicketTests(APITestCase):
    def setUp(self):
        # Create Roles
        self.admin_role = RoleFactory(name="Admin", hierarchy_level=80)
        self.agent_role = RoleFactory(name="Support Agent", hierarchy_level=40)
        self.student_role = RoleFactory(name="Student", is_default=True)

        # Assign Permissions
        _get_and_assign_permissions(
            self.admin_role,
            {
                SupportTicket: ["access_support_admin", "assign_support_ticket"],
            },
        )

        # Create Users
        self.admin_user = UserFactory(role=self.admin_role)
        self.agent_user = UserFactory(role=self.agent_role)
        self.student_user = UserFactory(role=self.student_role)

        # Add permissions directly to users for test client
        self.admin_user.user_permissions.add(*self.admin_role.permissions.all())
        self.agent_user.user_permissions.add(*self.agent_role.permissions.all())

        # Create a ticket for testing
        self.ticket = SupportTicketFactory(user=self.student_user)
        # Create the initial message that mirrors the real application logic
        TicketMessage.objects.create(
            ticket=self.ticket,
            sender=self.student_user,
            sender_type="user",
            text=self.ticket.description,
        )

    def test_admin_can_list_tickets(self):
        """
        GET /api/platform-admin/support-tickets/ - Admin can list all support tickets.
        """
        print("\n--- Running: test_admin_can_list_tickets ---")
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("admin-support-tickets-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["subject"], self.ticket.subject)
        print("✅ PASSED: Admin can list support tickets.")

    def test_admin_can_reply_to_ticket(self):
        """
        POST .../reply/ - Admin can reply to a ticket, creating a message and history log.
        """
        print("\n--- Running: test_admin_can_reply_to_ticket ---")
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("admin-support-tickets-reply", kwargs={"pk": self.ticket.pk})
        data = {"message": "Thank you for reaching out. We are looking into this."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.conversation.count(), 2)
        self.assertEqual(self.ticket.status, "in_progress")
        self.assertTrue(
            TicketHistoryLog.objects.filter(
                ticket=self.ticket, details="Replied to ticket."
            ).exists()
        )
        print("✅ PASSED: Admin reply created message and history log.")

    def test_admin_can_assign_ticket(self):
        """
        POST .../assign/ - Admin can assign a ticket to a support agent.
        """
        print("\n--- Running: test_admin_can_assign_ticket ---")
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("admin-support-tickets-assign", kwargs={"pk": self.ticket.pk})
        data = {"agent_id": self.agent_user.pk}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to, self.agent_user)
        self.assertEqual(self.ticket.status, "in_progress")
        self.assertTrue(
            TicketHistoryLog.objects.filter(
                ticket=self.ticket,
                details__contains="Assigned ticket from Unassigned to",
            ).exists()
        )
        print("✅ PASSED: Admin can assign a ticket.")

    def test_admin_can_resolve_ticket(self):
        """
        POST .../resolve/ - Admin can resolve a ticket with notes.
        """
        print("\n--- Running: test_admin_can_resolve_ticket ---")
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("admin-support-tickets-resolve", kwargs={"pk": self.ticket.pk})
        data = {
            "resolution_notes": "The issue was resolved by clearing the user's cache."
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "resolved")
        self.assertEqual(self.ticket.resolution_notes, data["resolution_notes"])
        self.assertIsNotNone(self.ticket.resolved_at)
        print("✅ PASSED: Admin can resolve a ticket.")

    def test_non_admin_cannot_list_tickets(self):
        """
        GET /api/platform-admin/support-tickets/ - A non-admin user cannot access the list.
        """
        print("\n--- Running: test_non_admin_cannot_list_tickets ---")
        self.client.force_authenticate(user=self.student_user)
        url = reverse("admin-support-tickets-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        print("✅ PASSED: Non-admin correctly denied access.")

    def test_assign_to_invalid_agent_fails(self):
        """
        [EDGE CASE] POST .../assign/ - Assigning to a non-existent agent ID fails.
        """
        print("\n--- Running: test_assign_to_invalid_agent_fails ---")
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("admin-support-tickets-assign", kwargs={"pk": self.ticket.pk})
        invalid_agent_id = self.agent_user.pk + 999
        data = {"agent_id": invalid_agent_id}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("agent_id", response.data)
        print("✅ PASSED: Assigning to an invalid agent correctly fails validation.")
