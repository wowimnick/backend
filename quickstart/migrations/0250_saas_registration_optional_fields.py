from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0249_corporateshortlist_presentation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="businessinfo",
            name="studentContactPhone",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AlterField(
            model_name="businessinfo",
            name="studentContactEmail",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
        migrations.AlterField(
            model_name="businessinfo",
            name="preferredContact",
            field=models.CharField(
                blank=True,
                choices=[
                    ("email", "Email"),
                    ("phone", "Phone"),
                    ("text", "Text Message"),
                    ("both", "Both Email and Phone"),
                ],
                default="email",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="businessinfo",
            name="businessDescription",
            field=models.TextField(blank=True, default="", max_length=750),
        ),
        migrations.AlterField(
            model_name="businessinfo",
            name="businessAddress",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="businessinfo",
            name="businessCity",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AlterField(
            model_name="businessinfo",
            name="businessState",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AlterField(
            model_name="businessinfo",
            name="businessZipCode",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="onboarding_completed",
            field=models.BooleanField(
                default=False,
                help_text="True after the SaaS signup wizard is finished or skipped past payouts/widget preview.",
            ),
        ),
    ]
