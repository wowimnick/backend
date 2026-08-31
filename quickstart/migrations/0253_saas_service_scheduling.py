from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import migrations, models
import django.core.validators
import django.db.models.deletion
import uuid


def backfill_service_defaults(apps, schema_editor):
    ClassesMain = apps.get_model("quickstart", "ClassesMain")
    Schedule = apps.get_model("quickstart", "Schedule")
    ClassOption = apps.get_model("quickstart", "ClassOption")

    for cls in ClassesMain.objects.all().iterator():
        rows = list(
            Schedule.objects.filter(option__classId_id=cls.pk).values(
                "duration", "price", "maxParticipants"
            )
        )
        update_fields = ["service_type"]
        cls.service_type = "group"
        if rows:
            durations = Counter(r["duration"] or 60 for r in rows)
            prices = Counter(r["price"] or Decimal("0.00") for r in rows)
            caps = Counter(r["maxParticipants"] or 1 for r in rows)
            cls.duration_minutes = durations.most_common(1)[0][0] or 60
            cls.price = prices.most_common(1)[0][0] or Decimal("0.00")
            cls.capacity = caps.most_common(1)[0][0] or 1
            update_fields.extend(["duration_minutes", "price", "capacity"])

        opt = (
            ClassOption.objects.filter(classId_id=cls.pk)
            .order_by("optionId")
            .first()
        )
        if opt:
            cls.cancellationPolicy = opt.cancellationPolicy or "flexible"
            cls.cancellationCustomHours = opt.cancellationCustomHours
            cls.cancellationRefundPercentage = (
                opt.cancellationRefundPercentage
                if opt.cancellationRefundPercentage is not None
                else 100
            )
            update_fields.extend(
                [
                    "cancellationPolicy",
                    "cancellationCustomHours",
                    "cancellationRefundPercentage",
                ]
            )
        cls.save(update_fields=update_fields)


def backfill_recurrence_rules(apps, schema_editor):
    RecurrenceRule = apps.get_model("quickstart", "RecurrenceRule")
    Schedule = apps.get_model("quickstart", "Schedule")
    ScheduleInstance = apps.get_model("quickstart", "ScheduleInstance")
    ClassesMain = apps.get_model("quickstart", "ClassesMain")

    groups = defaultdict(list)
    qs = Schedule.objects.filter(date__isnull=False).exclude(
        option__booking_type="Full Course"
    )
    for sched in qs.iterator():
        key = (
            sched.option_id,
            sched.name or "",
            sched.day or "",
            str(sched.time),
            sched.duration,
            str(sched.price),
            sched.maxParticipants,
        )
        groups[key].append(sched)

    for key, schedules in groups.items():
        if len(schedules) < 2:
            continue
        dates = sorted(s.date for s in schedules if s.date)
        if len(dates) < 2:
            continue
        first = schedules[0]
        option = first.option
        service_id = option.classId_id
        try:
            service = ClassesMain.objects.get(pk=service_id)
        except ClassesMain.DoesNotExist:
            continue
        tz = getattr(
            getattr(service, "businessId", None), "business_timezone", None
        ) or "America/Toronto"
        weekdays = sorted({(s.day or (s.date.strftime("%a") if s.date else "Mon")) for s in schedules})
        rule = RecurrenceRule.objects.create(
            service_id=service_id,
            variant_id=option.pk,
            weekdays=weekdays,
            time=first.time,
            duration_minutes=first.duration,
            price=first.price,
            capacity=first.maxParticipants,
            start_date=dates[0],
            until_date=dates[-1],
            timezone=tz,
            is_active=dates[-1] >= dates[0],
        )
        instance_ids = list(
            ScheduleInstance.objects.filter(schedule_id__in=[s.pk for s in schedules]).values_list(
                "pk", flat=True
            )
        )
        if instance_ids:
            ScheduleInstance.objects.filter(pk__in=instance_ids).update(
                recurrence_rule_id=rule.pk
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0252_saas_payout_crm_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="reminder_hours_before",
            field=models.PositiveIntegerField(
                default=24,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(168),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="classesmain",
            name="description",
            field=models.TextField(blank=True, default="", max_length=4000),
        ),
        migrations.AlterField(
            model_name="classesmain",
            name="location",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="classesmain",
            name="coordinates",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.AlterField(
            model_name="classesmain",
            name="features",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="service_type",
            field=models.CharField(
                choices=[("group", "Group session"), ("appointment", "Appointment")],
                db_index=True,
                default="group",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="duration_minutes",
            field=models.PositiveIntegerField(
                default=60,
                validators=[django.core.validators.MinValueValidator(5)],
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="price",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=10
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="capacity",
            field=models.PositiveIntegerField(
                default=1,
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="buffer_before_minutes",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="buffer_after_minutes",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="slot_interval_minutes",
            field=models.PositiveIntegerField(
                default=30,
                validators=[django.core.validators.MinValueValidator(5)],
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="max_concurrent",
            field=models.PositiveIntegerField(
                default=1,
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="min_notice_hours",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="max_advance_days",
            field=models.PositiveIntegerField(default=60),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="cancellationPolicy",
            field=models.CharField(
                choices=[
                    ("flexible", "Flexible (up to 1 hour before)"),
                    ("24h", "24 Hours Notice"),
                    ("48h", "48 Hours Notice"),
                    ("72h", "72 Hours Notice"),
                    ("strict", "Strict (Non-refundable)"),
                    ("custom", "Custom Notice Period"),
                ],
                default="flexible",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="cancellationCustomHours",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="classesmain",
            name="cancellationRefundPercentage",
            field=models.PositiveIntegerField(
                default=100,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
        migrations.AddField(
            model_name="classoption",
            name="duration_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="classoption",
            name="price",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True
            ),
        ),
        migrations.AddField(
            model_name="classoption",
            name="capacity",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="RecurrenceRule",
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
                ("weekdays", models.JSONField(default=list)),
                ("time", models.TimeField()),
                ("duration_minutes", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "price",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=10, null=True
                    ),
                ),
                ("capacity", models.PositiveIntegerField(blank=True, null=True)),
                ("start_date", models.DateField()),
                ("until_date", models.DateField(blank=True, null=True)),
                ("timezone", models.CharField(default="America/Toronto", max_length=50)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assigned_staff",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="recurrence_rules",
                        to="quickstart.businessstaff",
                    ),
                ),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recurrence_rules",
                        to="quickstart.classesmain",
                    ),
                ),
                (
                    "variant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recurrence_rules",
                        to="quickstart.classoption",
                    ),
                ),
            ],
            options={
                "db_table": "recurrence_rules",
            },
        ),
        migrations.CreateModel(
            name="ServiceAvailabilityWindow",
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
                    "weekday",
                    models.CharField(
                        choices=[
                            ("Mon", "Monday"),
                            ("Tue", "Tuesday"),
                            ("Wed", "Wednesday"),
                            ("Thu", "Thursday"),
                            ("Fri", "Friday"),
                            ("Sat", "Saturday"),
                            ("Sun", "Sunday"),
                        ],
                        max_length=3,
                    ),
                ),
                ("start_time", models.TimeField()),
                ("end_time", models.TimeField()),
                ("is_closed", models.BooleanField(default=False)),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="availability_windows",
                        to="quickstart.classesmain",
                    ),
                ),
            ],
            options={
                "db_table": "service_availability_windows",
                "ordering": ["weekday", "start_time"],
                "unique_together": {("service", "weekday", "start_time")},
            },
        ),
        migrations.CreateModel(
            name="BusinessTimeOff",
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
                ("title", models.CharField(default="Time off", max_length=150)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("start_time", models.TimeField(blank=True, null=True)),
                ("end_time", models.TimeField(blank=True, null=True)),
                ("all_day", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="time_off",
                        to="quickstart.businessinfo",
                    ),
                ),
            ],
            options={
                "db_table": "business_time_off",
                "ordering": ["-start_date"],
            },
        ),
        migrations.CreateModel(
            name="CalendarConnection",
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
                    "provider",
                    models.CharField(
                        choices=[
                            ("google", "Google Calendar"),
                            ("outlook", "Microsoft Outlook"),
                        ],
                        max_length=20,
                    ),
                ),
                ("calendar_id", models.CharField(blank=True, default="", max_length=255)),
                ("access_token", models.TextField(blank=True, default="")),
                ("refresh_token", models.TextField(blank=True, default="")),
                ("token_expires_at", models.DateTimeField(blank=True, null=True)),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("is_active", models.BooleanField(default=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="calendar_connections",
                        to="quickstart.businessinfo",
                    ),
                ),
            ],
            options={
                "db_table": "calendar_connections",
                "unique_together": {("business", "provider")},
            },
        ),
        migrations.AddField(
            model_name="scheduleinstance",
            name="recurrence_rule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="instances",
                to="quickstart.recurrencerule",
            ),
        ),
        migrations.AddField(
            model_name="scheduleinstance",
            name="assigned_staff",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_sessions",
                to="quickstart.businessstaff",
            ),
        ),
        migrations.AlterField(
            model_name="scheduleinstance",
            name="status",
            field=models.CharField(
                choices=[
                    ("scheduled", "Scheduled"),
                    ("cancelled", "Cancelled"),
                    ("completed", "Completed"),
                    ("blackout", "Blackout"),
                ],
                default="scheduled",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="attendance",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("attended", "Attended"),
                    ("no_show", "No-show"),
                ],
                db_index=True,
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="attendance_marked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="booking",
            name="calendar_event_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddIndex(
            model_name="recurrencerule",
            index=models.Index(
                fields=["service", "is_active"], name="recurrence__service_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="scheduleinstance",
            index=models.Index(
                fields=["recurrence_rule", "date"],
                name="sched_inst_rule_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="scheduleinstance",
            index=models.Index(
                fields=["assigned_staff", "date"],
                name="sched_inst_staff_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(fields=["attendance"], name="bookings_attendance_idx"),
        ),
        migrations.RunPython(backfill_service_defaults, noop_reverse),
        migrations.RunPython(backfill_recurrence_rules, noop_reverse),
    ]
