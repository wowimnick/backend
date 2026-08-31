"""Recurrence rules, availability windows, time-off, calendar connections, appointment slots."""

from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers

from quickstart.models import (
    Booking,
    BusinessInfo,
    BusinessStaff,
    BusinessTimeOff,
    CalendarConnection,
    ClassesMain,
    RecurrenceRule,
    ScheduleInstance,
    ServiceAvailabilityWindow,
)
from quickstart.services.availability import generate_appointment_slots
from quickstart.services.recurrence import (
    delete_this_and_following,
    edit_this_and_following,
    edit_this_session_only,
    materialize_rule,
)
from quickstart.utils.permissions import CanManageOwnClasses


def _business_for(user):
    business = BusinessInfo.objects.filter(
        Q(owner=user)
        | Q(staff_members__user=user, staff_members__status="accepted")
    ).first()
    if not business:
        raise NotFound("You are not a member of any business.")
    return business


class RecurrenceRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecurrenceRule
        fields = [
            "id",
            "service",
            "variant",
            "weekdays",
            "time",
            "duration_minutes",
            "price",
            "capacity",
            "start_date",
            "until_date",
            "timezone",
            "assigned_staff",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_weekdays(self, value):
        allowed = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
        if not value or not isinstance(value, list):
            raise serializers.ValidationError("Select at least one weekday.")
        cleaned = [d for d in value if d in allowed]
        if not cleaned:
            raise serializers.ValidationError("Select at least one weekday.")
        return cleaned


class ServiceAvailabilityWindowSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceAvailabilityWindow
        fields = ["id", "service", "weekday", "start_time", "end_time", "is_closed"]
        read_only_fields = ["id"]


class BusinessTimeOffSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessTimeOff
        fields = [
            "id",
            "title",
            "start_date",
            "end_date",
            "start_time",
            "end_time",
            "all_day",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class RecurrenceRuleViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]
    serializer_class = RecurrenceRuleSerializer

    def get_queryset(self):
        business = _business_for(self.request.user)
        qs = RecurrenceRule.objects.filter(service__businessId=business).select_related(
            "service", "variant", "assigned_staff"
        )
        service_id = self.request.query_params.get("service_id") or self.request.query_params.get(
            "class_id"
        )
        if service_id:
            qs = qs.filter(service_id=service_id)
        return qs.order_by("-start_date")

    def perform_create(self, serializer):
        business = _business_for(self.request.user)
        service = serializer.validated_data["service"]
        if service.businessId_id != business.businessId:
            raise PermissionDenied("Service does not belong to your business.")
        if service.service_type == "appointment":
            raise ValidationError(
                "Recurring series apply to group services. Appointment services use availability instead."
            )
        tz = serializer.validated_data.get("timezone") or business.business_timezone
        rule = serializer.save(timezone=tz)
        materialize_rule(rule)

    def perform_update(self, serializer):
        rule = serializer.save()
        materialize_rule(rule)


class RecurrenceRuleMaterializeView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, pk):
        business = _business_for(request.user)
        rule = get_object_or_404(RecurrenceRule, pk=pk, service__businessId=business)
        created = materialize_rule(rule)
        return Response({"created_count": len(created)})


class SessionEditScopeView(APIView):
    """Edit or delete a session: scope=this | following."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, pk):
        business = _business_for(request.user)
        instance = get_object_or_404(
            ScheduleInstance,
            pk=pk,
            schedule__option__classId__businessId=business,
        )
        scope = (request.data.get("scope") or "this").lower()
        action = (request.data.get("action") or "edit").lower()
        if action == "delete":
            reason = request.data.get("reason") or "Cancelled by business"
            if scope == "following":
                count = delete_this_and_following(instance, reason=reason)
                return Response({"cancelled_count": count})
            instance.status = "cancelled"
            instance.cancellation_reason = reason
            instance.save(update_fields=["status", "cancellation_reason"])
            return Response({"cancelled_count": 1})

        changes = {}
        for field in ("time", "duration", "price", "max_participants", "date"):
            if field in request.data and request.data.get(field) not in (None, ""):
                changes[field] = request.data.get(field)
        if "assigned_staff_id" in request.data:
            staff_id = request.data.get("assigned_staff_id")
            if staff_id:
                staff = get_object_or_404(
                    BusinessStaff, pk=staff_id, business=business, status="accepted"
                )
                changes["assigned_staff"] = staff
            else:
                changes["assigned_staff"] = None
        if "time" in changes and isinstance(changes["time"], str):
            changes["time"] = datetime.strptime(changes["time"][:8], "%H:%M:%S").time() if len(changes["time"]) >= 8 else datetime.strptime(changes["time"][:5], "%H:%M").time()
        if "date" in changes and isinstance(changes["date"], str):
            changes["date"] = datetime.strptime(changes["date"], "%Y-%m-%d").date()
        if "duration" in changes:
            changes["duration"] = int(changes["duration"])
        if "max_participants" in changes:
            changes["max_participants"] = int(changes["max_participants"])
        if "price" in changes:
            changes["price"] = Decimal(str(changes["price"]))

        if scope == "following":
            edit_this_and_following(instance, **changes)
        else:
            edit_this_session_only(instance, **changes)
        instance.refresh_from_db()
        return Response(
            {
                "id": instance.id,
                "date": instance.date.isoformat(),
                "time": instance.time.strftime("%H:%M:%S"),
                "duration": instance.duration,
                "price": str(instance.price),
                "max_participants": instance.max_participants,
                "recurrence_rule_id": str(instance.recurrence_rule_id)
                if instance.recurrence_rule_id
                else None,
            }
        )


class ServiceAvailabilityWindowListView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, class_id):
        business = _business_for(request.user)
        service = get_object_or_404(ClassesMain, pk=class_id, businessId=business)
        rows = ServiceAvailabilityWindow.objects.filter(service=service)
        return Response(ServiceAvailabilityWindowSerializer(rows, many=True).data)

    def put(self, request, class_id):
        business = _business_for(request.user)
        service = get_object_or_404(ClassesMain, pk=class_id, businessId=business)
        payload = request.data if isinstance(request.data, list) else request.data.get("windows") or []
        ServiceAvailabilityWindow.objects.filter(service=service).delete()
        created = []
        for row in payload:
            row = dict(row)
            row["service"] = service.pk
            ser = ServiceAvailabilityWindowSerializer(data=row)
            ser.is_valid(raise_exception=True)
            created.append(ser.save())
        return Response(ServiceAvailabilityWindowSerializer(created, many=True).data)


class AppointmentSlotsView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, class_id):
        business = _business_for(request.user)
        service = get_object_or_404(ClassesMain, pk=class_id, businessId=business)
        start = request.query_params.get("start_date")
        end = request.query_params.get("end_date")
        if not start or not end:
            raise ValidationError("start_date and end_date are required.")
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end, "%Y-%m-%d").date()
        slots = generate_appointment_slots(service, start_d, end_d)
        return Response({"slots": slots})


class BusinessTimeOffListCreateView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request):
        business = _business_for(request.user)
        qs = BusinessTimeOff.objects.filter(business=business)
        start = request.query_params.get("start_date")
        end = request.query_params.get("end_date")
        if start:
            qs = qs.filter(end_date__gte=start)
        if end:
            qs = qs.filter(start_date__lte=end)
        return Response(BusinessTimeOffSerializer(qs, many=True).data)

    def post(self, request):
        business = _business_for(request.user)
        ser = BusinessTimeOffSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(business=business)
        return Response(ser.data, status=status.HTTP_201_CREATED)


class BusinessTimeOffDetailView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def delete(self, request, pk):
        business = _business_for(request.user)
        row = get_object_or_404(BusinessTimeOff, pk=pk, business=business)
        row.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CalendarConnectionListView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request):
        business = _business_for(request.user)
        rows = CalendarConnection.objects.filter(business=business)
        return Response(
            [
                {
                    "id": str(c.id),
                    "provider": c.provider,
                    "email": c.email,
                    "calendar_id": c.calendar_id,
                    "is_active": c.is_active,
                    "last_error": c.last_error,
                    "last_synced_at": c.last_synced_at.isoformat() if c.last_synced_at else None,
                    "needs_reconnect": bool(c.last_error),
                }
                for c in rows
            ]
        )


class CalendarConnectionDisconnectView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, pk):
        business = _business_for(request.user)
        row = get_object_or_404(CalendarConnection, pk=pk, business=business)
        row.is_active = False
        row.access_token = ""
        row.refresh_token = ""
        row.save(update_fields=["is_active", "access_token", "refresh_token", "updated_at"])
        return Response({"ok": True})


class CalendarOAuthStartView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, provider):
        if provider not in ("google", "outlook"):
            raise ValidationError("provider must be google or outlook.")
        from django.conf import settings
        from urllib.parse import urlencode

        redirect_uri = request.build_absolute_uri(
            f"/api/my-business/calendar/oauth/{provider}/callback/"
        )
        state = str(_business_for(request.user).businessId)
        if provider == "google":
            client_id = getattr(settings, "GOOGLE_CALENDAR_CLIENT_ID", "") or getattr(
                settings, "GOOGLE_OAUTH_CLIENT_ID", ""
            )
            if not client_id:
                return Response(
                    {"error": "Google Calendar is not configured on this server."},
                    status=status.HTTP_501_NOT_IMPLEMENTED,
                )
            params = urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "access_type": "offline",
                    "prompt": "consent",
                    "scope": "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/userinfo.email",
                    "state": state,
                }
            )
            return Response({"authorize_url": f"https://accounts.google.com/o/oauth2/v2/auth?{params}"})
        client_id = getattr(settings, "MICROSOFT_CALENDAR_CLIENT_ID", "") or getattr(
            settings, "MS_GRAPH_CLIENT_ID", ""
        )
        if not client_id:
            return Response(
                {"error": "Outlook Calendar is not configured on this server."},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )
        params = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "response_mode": "query",
                "scope": "offline_access Calendars.ReadWrite User.Read",
                "state": state,
            }
        )
        return Response(
            {
                "authorize_url": f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{params}"
            }
        )


class CalendarOAuthCallbackView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, provider):
        import requests
        from django.conf import settings

        business = _business_for(request.user)
        code = request.query_params.get("code")
        if not code:
            raise ValidationError("Missing OAuth code.")
        redirect_uri = request.build_absolute_uri(
            f"/api/my-business/calendar/oauth/{provider}/callback/"
        )
        if provider == "google":
            client_id = getattr(settings, "GOOGLE_CALENDAR_CLIENT_ID", "") or getattr(
                settings, "GOOGLE_OAUTH_CLIENT_ID", ""
            )
            client_secret = getattr(settings, "GOOGLE_CALENDAR_CLIENT_SECRET", "") or getattr(
                settings, "GOOGLE_OAUTH_CLIENT_SECRET", ""
            )
            token_url = "https://oauth2.googleapis.com/token"
        else:
            client_id = getattr(settings, "MICROSOFT_CALENDAR_CLIENT_ID", "") or getattr(
                settings, "MS_GRAPH_CLIENT_ID", ""
            )
            client_secret = getattr(settings, "MICROSOFT_CALENDAR_CLIENT_SECRET", "") or getattr(
                settings, "MS_GRAPH_CLIENT_SECRET", ""
            )
            token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        resp = requests.post(
            token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        expires_in = int(data.get("expires_in") or 3600)
        connection, _ = CalendarConnection.objects.update_or_create(
            business=business,
            provider=provider,
            defaults={
                "access_token": data.get("access_token") or "",
                "refresh_token": data.get("refresh_token") or "",
                "token_expires_at": timezone.now() + timedelta(seconds=expires_in),
                "is_active": True,
                "last_error": "",
            },
        )
        return Response({"ok": True, "provider": provider, "id": str(connection.id)})
