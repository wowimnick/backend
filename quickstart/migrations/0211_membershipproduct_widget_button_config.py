# Embed button targeting / styling for Sell Memberships snippets (per product).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0210_membership_product_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="membershipproduct",
            name="widget_button_config",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Optional: open_class_id, button_label, button_background, button_text_color, button_radius_px for website embed snippet.",
            ),
        ),
    ]
