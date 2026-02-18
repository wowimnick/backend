# Guest–business messaging: Conversation, ConversationMessage, ConversationEmailLog

import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0194_add_access_global_discount_admin_permission"),
    ]

    operations = [
        migrations.CreateModel(
            name="Conversation",
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
                (
                    "last_message_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        help_text="Updated when a message is added; used for sorting.",
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "booking",
                    models.ForeignKey(
                        blank=True,
                        help_text="Optional booking context for this conversation.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="conversations",
                        to="quickstart.booking",
                    ),
                ),
                (
                    "booker_contact",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversations",
                        to="quickstart.contact",
                    ),
                ),
                (
                    "booker_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="guest_conversations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conversations",
                        to="quickstart.businessinfo",
                    ),
                ),
            ],
            options={
                "db_table": "conversations",
                "ordering": ["-last_message_at", "-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ConversationMessage",
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
                (
                    "sender_type",
                    models.CharField(
                        choices=[
                            ("booker", "Booker"),
                            ("business", "Business"),
                        ],
                        db_index=True,
                        max_length=10,
                    ),
                ),
                ("text", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="quickstart.conversation",
                    ),
                ),
                (
                    "sender_contact",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="conversation_messages_sent",
                        to="quickstart.contact",
                    ),
                ),
                (
                    "sender_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="conversation_messages_sent",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "conversation_messages",
                "ordering": ["created_at"],
            },
        ),
        migrations.CreateModel(
            name="ConversationEmailLog",
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
                (
                    "recipient_side",
                    models.CharField(
                        choices=[
                            ("booker", "Booker"),
                            ("business", "Business"),
                        ],
                        db_index=True,
                        max_length=10,
                    ),
                ),
                ("last_email_sent_at", models.DateTimeField(db_index=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="email_logs",
                        to="quickstart.conversation",
                    ),
                ),
            ],
            options={
                "db_table": "conversation_email_logs",
            },
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                condition=models.Q(booker_user__isnull=False),
                fields=("business", "booker_user"),
                name="unique_conversation_business_booker_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                condition=models.Q(booker_contact__isnull=False),
                fields=("business", "booker_contact"),
                name="unique_conversation_business_booker_contact",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="conversationemaillog",
            unique_together={("conversation", "recipient_side")},
        ),
    ]
