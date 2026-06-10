import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0244_class_slug_redirect"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="customuser",
            options={
                "permissions": [
                    ("change_user_role", "Can change the role assigned to any user"),
                    ("lock_user", "Can lock/unlock any user account"),
                    ("manage_ip_bans", "Can manage IP bans"),
                    ("reset_user_password", "Can initiate password reset for any user"),
                    ("view_user_metrics", "Can view user management metrics"),
                    ("access_user_admin", "Can access the user administration section"),
                    ("view_system_metrics", "Can view real-time system performance metrics"),
                    (
                        "access_admin_dashboard",
                        "Can access the main platform administration dashboard",
                    ),
                    ("access_blog_admin", "Can access the Blog Management section"),
                    (
                        "access_global_discount_admin",
                        "Can access Global Discount management",
                    ),
                    ("reply_own_support_ticket", "Can reply to own support tickets"),
                    ("cancel_own_booking", "Can cancel their own booking"),
                    ("add_supportticket", "Can create new support tickets"),
                ]
            },
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="moderated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="moderation_confidence",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="moderation_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="moderation_status",
            field=models.CharField(
                choices=[
                    ("approved", "Approved"),
                    ("pending", "Pending"),
                    ("rejected", "Rejected"),
                    ("error", "Error"),
                ],
                db_index=True,
                default="approved",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="conversationmessage",
            name="sender_ip",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="BannedIP",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("ip_address", models.GenericIPAddressField(db_index=True)),
                ("reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="banned_ips_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "banned_ips",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["is_active", "expires_at"],
                        name="banned_ips_is_acti_idx",
                    )
                ],
            },
        ),
    ]
