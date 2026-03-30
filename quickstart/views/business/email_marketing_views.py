"""Authenticated email marketing CRUD + public unsubscribe."""
import logging
import re

import resend
from django.conf import settings
from django.core.signing import BadSignature, Signer
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from quickstart.models import (
    ADDON_TYPE_EMAIL_MARKETING,
    BusinessEmailCampaign,
    BusinessMarketingSettings,
    CampaignEmailSend,
    EmailMarketingTemplate,
    MarketingSavedSegment,
    MarketingSenderProfile,
    MarketingSendingDomain,
)
from quickstart.services.email_marketing_config import price_id_to_tier
from quickstart.services.marketing_audience import (
    audience_tier_error,
    build_contact_queryset,
    serialize_contact_sample,
)
from quickstart.services.marketing_builder import resolve_campaign_html_body
from quickstart.services.email_marketing_usage import (
    MarketingQuotaExceeded,
    assert_can_send,
    usage_snapshot,
)
from quickstart.services.resend_domain_service import (
    resend_create_domain,
    resend_remove_domain,
    resend_verify_domain,
)
from quickstart.services.subscription_sync import sync_addon_subscription_from_stripe
from quickstart.tasks.email_marketing_tasks import (
    _footer_html,
    _merge_fields,
    _resolve_from_header,
    send_business_marketing_campaign_task,
)
from quickstart.tasks.email_tasks import rate_limiter
from quickstart.utils.marketing_html import sanitize_marketing_html
from quickstart.utils.permissions import CanManageEmailMarketing
from quickstart.views.widget.widget_config_views import (
    _get_business_for_subscription,
    resolve_email_marketing_addon_subscription,
)

logger = logging.getLogger(__name__)

_UNSUB_SIGNER = Signer(salt="ce-marketing-unsub-v1")


def _email_domain(email):
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[-1].strip().lower()


def _active_addon(business):
    """
    Resolve addon the same way as BusinessAddonsView, then refresh business so
    email_marketing_enabled matches what Stripe sync wrote (avoids stale ORM).
    """
    sub = resolve_email_marketing_addon_subscription(business)
    business.refresh_from_db(fields=["email_marketing_enabled"])
    return sub


def _require_marketing(request):
    business = _get_business_for_subscription(request.user)
    addon = _active_addon(business)
    if not addon or not getattr(business, "email_marketing_enabled", False):
        return None, None, Response(
            {"error": "Email marketing is not active for this business."},
            status=status.HTTP_403_FORBIDDEN,
        )
    tier = price_id_to_tier(addon.stripe_price_id)
    if not tier:
        return None, None, Response(
            {"error": "Email marketing plan is not recognized."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return business, addon, None


class MarketingAccountView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business = _get_business_for_subscription(request.user)
        addon = _active_addon(business)
        snap = usage_snapshot(business, addon) if addon else None
        tier_api = None
        if addon and business.email_marketing_enabled:
            raw_tier = price_id_to_tier(addon.stripe_price_id)
            if raw_tier:
                tier_api = {k: v for k, v in raw_tier.items() if k != "price_id"}
        return Response(
            {
                "active": addon is not None and business.email_marketing_enabled,
                "usage": snap,
                "tier": tier_api,
                "transactional_emails_excluded_from_quota": True,
            }
        )


class MarketingSettingsDetailView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business = _get_business_for_subscription(request.user)
        obj, _ = BusinessMarketingSettings.objects.get_or_create(business=business)
        return Response(
            {
                "physical_address_footer": obj.physical_address_footer,
            }
        )

    def patch(self, request):
        business, _, err = _require_marketing(request)
        if err:
            return err
        obj, _ = BusinessMarketingSettings.objects.get_or_create(business=business)
        body = request.data.get("physical_address_footer")
        if body is not None:
            obj.physical_address_footer = str(body)[:2000]
            obj.save(update_fields=["physical_address_footer"])
        return Response({"physical_address_footer": obj.physical_address_footer})


class MarketingTemplateListCreateView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        qs = EmailMarketingTemplate.objects.filter(business=business)
        return Response(
            [
                {
                    "id": str(t.id),
                    "name": t.name,
                    "subject": t.subject,
                    "content_type": t.content_type,
                    "updated_at": t.updated_at.isoformat(),
                }
                for t in qs
            ]
        )

    def post(self, request):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        n = EmailMarketingTemplate.objects.filter(business=business).count()
        if n >= tier["max_saved_templates"]:
            return Response(
                {"error": f"Template limit reached ({tier['max_saved_templates']})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        name = (request.data.get("name") or "").strip() or "Untitled"
        subject = (request.data.get("subject") or "").strip()
        content_type = (request.data.get("content_type") or "html").strip()
        if content_type not in ("html", "builder_json"):
            content_type = "html"
        if content_type == "html" and not tier.get("raw_html_allowed", True):
            return Response({"error": "HTML content not allowed on this tier."}, status=400)
        html_body = sanitize_marketing_html(request.data.get("html_body") or "")
        builder_json = request.data.get("builder_json") or {}
        if isinstance(builder_json, str):
            builder_json = {}
        t = EmailMarketingTemplate.objects.create(
            business=business,
            name=name[:200],
            subject=subject[:255],
            content_type=content_type,
            html_body=html_body,
            builder_json=builder_json if content_type == "builder_json" else {},
        )
        return Response({"id": str(t.id)}, status=status.HTTP_201_CREATED)


class MarketingTemplateDetailView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request, template_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        t = get_object_or_404(EmailMarketingTemplate, id=template_id, business=business)
        return Response(
            {
                "id": str(t.id),
                "name": t.name,
                "subject": t.subject,
                "content_type": t.content_type,
                "html_body": t.html_body,
                "builder_json": t.builder_json,
            }
        )

    def put(self, request, template_id):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        t = get_object_or_404(EmailMarketingTemplate, id=template_id, business=business)
        if request.data.get("name") is not None:
            t.name = str(request.data.get("name"))[:200]
        if request.data.get("subject") is not None:
            t.subject = str(request.data.get("subject"))[:255]
        if request.data.get("content_type") is not None:
            t.content_type = str(request.data.get("content_type"))[:32]
        if request.data.get("html_body") is not None:
            t.html_body = sanitize_marketing_html(request.data.get("html_body") or "")
        if request.data.get("builder_json") is not None and isinstance(
            request.data.get("builder_json"), dict
        ):
            t.builder_json = request.data.get("builder_json")
        if t.content_type == "html" and not tier.get("raw_html_allowed", True):
            return Response({"error": "HTML not allowed on this tier."}, status=400)
        t.save()
        return Response({"id": str(t.id)})

    def delete(self, request, template_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        t = get_object_or_404(EmailMarketingTemplate, id=template_id, business=business)
        t.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MarketingSenderListCreateView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business, _, err = _require_marketing(request)
        if err:
            return err
        qs = MarketingSenderProfile.objects.filter(business=business)
        return Response(
            [
                {
                    "id": str(s.id),
                    "display_name": s.display_name,
                    "from_email": s.from_email,
                    "reply_to": s.reply_to,
                    "is_default": s.is_default,
                }
                for s in qs
            ]
        )

    def post(self, request):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        display_name = (request.data.get("display_name") or "").strip() or business.businessName
        from_email = (request.data.get("from_email") or "").strip().lower()
        if not from_email:
            return Response({"error": "from_email is required."}, status=400)
        dom = _email_domain(from_email)
        if tier.get("custom_domain_allowed"):
            ok = MarketingSendingDomain.objects.filter(
                business=business, domain=dom, status="verified"
            ).exists()
            if not ok:
                return Response(
                    {"error": "from_email must use a verified sending domain for this tier."},
                    status=400,
                )
        reply_to = (request.data.get("reply_to") or "").strip() or None
        s = MarketingSenderProfile.objects.create(
            business=business,
            display_name=display_name[:200],
            from_email=from_email,
            reply_to=reply_to,
            is_default=bool(request.data.get("is_default")),
        )
        if s.is_default:
            MarketingSenderProfile.objects.filter(business=business).exclude(pk=s.pk).update(
                is_default=False
            )
        return Response({"id": str(s.id)}, status=201)


class MarketingSenderDetailView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def delete(self, request, sender_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        s = get_object_or_404(MarketingSenderProfile, id=sender_id, business=business)
        s.delete()
        return Response(status=204)


class MarketingDomainListCreateView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business, _, err = _require_marketing(request)
        if err:
            return err
        qs = MarketingSendingDomain.objects.filter(business=business)
        return Response(
            [
                {
                    "id": str(d.id),
                    "domain": d.domain,
                    "status": d.status,
                    "resend_domain_id": d.resend_domain_id,
                    "dns_records": d.dns_records,
                }
                for d in qs
            ]
        )

    def post(self, request):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        if not tier.get("custom_domain_allowed"):
            return Response(
                {"error": "Custom sending domains are not included in your plan."},
                status=403,
            )
        domain = (request.data.get("domain") or "").strip().lower()
        domain = re.sub(r"^https?://", "", domain).split("/")[0].strip()
        if not domain or "." not in domain:
            return Response({"error": "Invalid domain."}, status=400)
        if MarketingSendingDomain.objects.filter(domain=domain).exists():
            return Response({"error": "This domain is already registered."}, status=409)
        try:
            raw = resend_create_domain(domain)
        except Exception as e:
            logger.exception("resend_create_domain: %s", e)
            return Response({"error": "Could not create domain in email provider."}, status=502)
        rid = getattr(raw, "id", None) or (raw.get("id") if isinstance(raw, dict) else "")
        rec = getattr(raw, "records", None) or (raw.get("records") if isinstance(raw, dict) else [])
        d = MarketingSendingDomain.objects.create(
            business=business,
            domain=domain,
            resend_domain_id=rid or "",
            status="pending",
            dns_records=list(rec) if rec else [],
        )
        return Response(
            {
                "id": str(d.id),
                "domain": d.domain,
                "status": d.status,
                "resend_domain_id": d.resend_domain_id,
                "dns_records": d.dns_records,
            },
            status=201,
        )


class MarketingDomainVerifyView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def post(self, request, domain_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        d = get_object_or_404(MarketingSendingDomain, id=domain_id, business=business)
        if not d.resend_domain_id:
            return Response({"error": "Domain not linked to provider."}, status=400)
        try:
            raw = resend_verify_domain(d.resend_domain_id)
        except Exception as e:
            logger.warning("resend_verify_domain: %s", e)
            return Response({"error": "Verification request failed."}, status=502)
        st = getattr(raw, "status", None) or (raw.get("status") if isinstance(raw, dict) else "")
        d.status = "verified" if str(st).lower() == "verified" else "pending"
        d.save(update_fields=["status", "updated_at"])
        return Response({"status": d.status})


class MarketingDomainDeleteView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def delete(self, request, domain_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        d = get_object_or_404(MarketingSendingDomain, id=domain_id, business=business)
        if d.resend_domain_id:
            try:
                resend_remove_domain(d.resend_domain_id)
            except Exception as e:
                logger.warning("resend_remove_domain: %s", e)
        d.delete()
        return Response(status=204)


class MarketingCampaignListCreateView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business, _, err = _require_marketing(request)
        if err:
            return err
        qs = (
            BusinessEmailCampaign.objects.filter(business=business)
            .annotate(
                sends_sent=Count("sends", filter=Q(sends__status="sent")),
                sends_failed=Count("sends", filter=Q(sends__status="failed")),
            )
            .order_by("-created_at")
        )
        return Response(
            [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "status": c.status,
                    "subject": c.subject,
                    "content_type": c.content_type,
                    "recipient_count": c.recipient_count,
                    "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
                    "sent_at": c.sent_at.isoformat() if c.sent_at else None,
                    "updated_at": c.updated_at.isoformat(),
                    "sends_sent": c.sends_sent,
                    "sends_failed": c.sends_failed,
                }
                for c in qs
            ]
        )

    def post(self, request):
        business, _, err = _require_marketing(request)
        if err:
            return err
        name = (request.data.get("name") or "").strip() or "Untitled campaign"
        c = BusinessEmailCampaign.objects.create(
            business=business,
            name=name[:255],
            created_by=request.user,
        )
        return Response({"id": str(c.id)}, status=201)


class MarketingCampaignDetailView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request, campaign_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        c = get_object_or_404(BusinessEmailCampaign, id=campaign_id, business=business)
        sends_sent = CampaignEmailSend.objects.filter(
            campaign=c, status="sent"
        ).count()
        sends_failed = CampaignEmailSend.objects.filter(
            campaign=c, status="failed"
        ).count()
        return Response(
            {
                "id": str(c.id),
                "name": c.name,
                "status": c.status,
                "subject": c.subject,
                "html_body": c.html_body,
                "builder_json": c.builder_json,
                "content_type": c.content_type,
                "audience_type": c.audience_type,
                "audience_filter": c.audience_filter,
                "sender_profile": str(c.sender_profile_id) if c.sender_profile_id else None,
                "recipient_count": c.recipient_count,
                "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
                "sent_at": c.sent_at.isoformat() if c.sent_at else None,
                "error_message": c.error_message,
                "sends_sent": sends_sent,
                "sends_failed": sends_failed,
            }
        )

    def put(self, request, campaign_id):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        c = get_object_or_404(BusinessEmailCampaign, id=campaign_id, business=business)
        if c.status not in ("draft", "failed", "scheduled"):
            return Response({"error": "Campaign is not editable."}, status=400)
        if request.data.get("name") is not None:
            c.name = str(request.data.get("name"))[:255]
        if request.data.get("subject") is not None:
            c.subject = str(request.data.get("subject"))[:255]
        if request.data.get("html_body") is not None:
            c.html_body = sanitize_marketing_html(request.data.get("html_body") or "")
        if request.data.get("builder_json") is not None and isinstance(
            request.data.get("builder_json"), dict
        ):
            c.builder_json = request.data.get("builder_json")
        if request.data.get("content_type") is not None:
            c.content_type = str(request.data.get("content_type"))[:32]
        if request.data.get("audience_type") is not None:
            c.audience_type = str(request.data.get("audience_type"))[:64]
        if request.data.get("audience_filter") is not None and isinstance(
            request.data.get("audience_filter"), dict
        ):
            c.audience_filter = request.data.get("audience_filter")
        sp = request.data.get("sender_profile")
        if sp:
            c.sender_profile = get_object_or_404(
                MarketingSenderProfile, id=sp, business=business
            )
        if request.data.get("scheduled_at") is not None:
            raw_sa = request.data.get("scheduled_at")
            if raw_sa in ("", None, False):
                c.scheduled_at = None
                if c.status == "scheduled":
                    c.status = "draft"
            else:
                if not tier.get("scheduling_enabled"):
                    return Response(
                        {"error": "Scheduled sending requires Growth or higher email marketing."},
                        status=403,
                    )
                parsed = parse_datetime(str(raw_sa))
                if not parsed:
                    return Response({"error": "Invalid scheduled_at."}, status=400)
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
                c.scheduled_at = parsed
        if c.content_type == "html" and not tier.get("raw_html_allowed", True):
            return Response({"error": "HTML not allowed on this tier."}, status=400)
        c.save()
        return Response({"id": str(c.id)})

    def delete(self, request, campaign_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        c = get_object_or_404(BusinessEmailCampaign, id=campaign_id, business=business)
        if c.status == "sending":
            return Response({"error": "Cannot delete while sending."}, status=400)
        c.delete()
        return Response(status=204)


class MarketingAudiencePreviewView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def post(self, request):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        at = (request.data.get("audience_type") or "all_contacts").strip()
        flt = request.data.get("audience_filter") or {}
        if not isinstance(flt, dict):
            flt = {}
        tier_err = audience_tier_error(tier, at, flt)
        if tier_err:
            return Response({"error": tier_err}, status=403)
        qs, meta = build_contact_queryset(business, at, flt)
        count = qs.count()
        sample_qs = qs[:8]
        return Response(
            {
                "count": count,
                "sample": [serialize_contact_sample(x) for x in sample_qs],
                "meta": meta,
            }
        )


class MarketingAudienceFacetsView(APIView):
    """Return selectable values for audience filters: contact tags and classes."""

    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business, _, err = _require_marketing(request)
        if err:
            return err

        from quickstart.models import Contact, ClassesMain

        # Distinct tags used across this business's contacts (cap at 300)
        contacts_with_tags = (
            Contact.objects.filter(business=business)
            .exclude(tags=[])
            .values_list("tags", flat=True)[:2000]
        )
        tag_set = set()
        for tag_list in contacts_with_tags:
            if isinstance(tag_list, list):
                for t in tag_list:
                    if t and isinstance(t, str):
                        tag_set.add(t.lower().strip())
        tags = sorted(tag_set)[:300]

        # Distinct contact sources used by this business
        sources = sorted(
            Contact.objects.filter(business=business)
            .exclude(source__isnull=True)
            .exclude(source="")
            .values_list("source", flat=True)
            .distinct()[:100]
        )

        # Classes offered by this business (id + title)
        classes = list(
            ClassesMain.objects.filter(businessId=business, status="active")
            .order_by("title")
            .values("classId", "title")[:200]
        )

        return Response(
            {
                "tags": tags,
                "sources": sources,
                "classes": [{"id": c["classId"], "title": c["title"]} for c in classes],
            }
        )


class MarketingSavedSegmentListCreateView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        if not tier.get("saved_segments_enabled"):
            return Response({"error": "Saved audiences require Growth or higher."}, status=403)
        qs = MarketingSavedSegment.objects.filter(business=business)
        return Response(
            [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "audience_type": s.audience_type,
                    "audience_filter": s.audience_filter,
                    "updated_at": s.updated_at.isoformat(),
                }
                for s in qs
            ]
        )

    def post(self, request):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        if not tier.get("saved_segments_enabled"):
            return Response({"error": "Saved audiences require Growth or higher."}, status=403)
        name = (request.data.get("name") or "").strip() or "Untitled audience"
        at = (request.data.get("audience_type") or "all_contacts").strip()
        flt = request.data.get("audience_filter") or {}
        if not isinstance(flt, dict):
            flt = {}
        tier_err = audience_tier_error(tier, at, flt)
        if tier_err:
            return Response({"error": tier_err}, status=403)
        seg = MarketingSavedSegment.objects.create(
            business=business,
            name=name[:255],
            audience_type=at[:64],
            audience_filter=flt,
        )
        return Response({"id": str(seg.id)}, status=201)


class MarketingSavedSegmentDetailView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def put(self, request, segment_id):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        if not tier.get("saved_segments_enabled"):
            return Response({"error": "Saved audiences require Growth or higher."}, status=403)
        seg = get_object_or_404(MarketingSavedSegment, id=segment_id, business=business)
        if request.data.get("name") is not None:
            seg.name = str(request.data.get("name"))[:255]
        if request.data.get("audience_type") is not None:
            seg.audience_type = str(request.data.get("audience_type"))[:64]
        if request.data.get("audience_filter") is not None and isinstance(
            request.data.get("audience_filter"), dict
        ):
            seg.audience_filter = request.data.get("audience_filter")
        tier_err = audience_tier_error(tier, seg.audience_type, seg.audience_filter or {})
        if tier_err:
            return Response({"error": tier_err}, status=403)
        seg.save()
        return Response({"id": str(seg.id)})

    def delete(self, request, segment_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        seg = get_object_or_404(MarketingSavedSegment, id=segment_id, business=business)
        seg.delete()
        return Response(status=204)


class MarketingCampaignScheduleView(APIView):
    """Set status=scheduled with future scheduled_at, or cancel schedule."""

    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def post(self, request, campaign_id):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        if not tier.get("scheduling_enabled"):
            return Response(
                {"error": "Scheduled sending requires Growth or higher email marketing."},
                status=403,
            )
        c = get_object_or_404(BusinessEmailCampaign, id=campaign_id, business=business)
        if c.status not in ("draft", "failed", "scheduled"):
            return Response({"error": "Campaign cannot be scheduled."}, status=400)
        if request.data.get("cancel"):
            c.scheduled_at = None
            if c.status == "scheduled":
                c.status = "draft"
            c.save(update_fields=["scheduled_at", "status", "updated_at"])
            return Response({"status": c.status})

        if not (c.subject or "").strip():
            return Response({"error": "Subject is required."}, status=400)
        if not (resolve_campaign_html_body(c) or "").strip():
            return Response({"error": "Email body is required."}, status=400)

        raw_sa = request.data.get("scheduled_at")
        parsed = parse_datetime(str(raw_sa)) if raw_sa else None
        if not parsed:
            return Response({"error": "scheduled_at is required (ISO 8601)."}, status=400)
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        now = timezone.now()
        if parsed <= now:
            return Response({"error": "scheduled_at must be in the future."}, status=400)

        at = (c.audience_type or "all_contacts").strip()
        flt = c.audience_filter or {}
        tier_err = audience_tier_error(tier, at, flt)
        if tier_err:
            return Response({"error": tier_err}, status=403)
        qs, _ = build_contact_queryset(business, at, flt)
        n = qs.count()
        try:
            assert_can_send(business, addon, n)
        except MarketingQuotaExceeded as e:
            return Response(
                {"error": str(e), "used": e.used, "limit": e.limit},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        c.scheduled_at = parsed
        c.status = "scheduled"
        c.error_message = ""
        c.save(update_fields=["scheduled_at", "status", "error_message", "updated_at"])
        return Response(
            {"status": "scheduled", "scheduled_at": c.scheduled_at.isoformat(), "recipient_count": n}
        )


class MarketingCampaignSendView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def post(self, request, campaign_id):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        c = get_object_or_404(BusinessEmailCampaign, id=campaign_id, business=business)
        if c.status == "scheduled":
            c.scheduled_at = None
            c.status = "draft"
            c.save(update_fields=["scheduled_at", "status", "updated_at"])
        if c.status not in ("draft", "failed"):
            return Response({"error": "Campaign already sent or in progress."}, status=400)
        if not (c.subject or "").strip():
            return Response({"error": "Subject is required."}, status=400)
        if not (resolve_campaign_html_body(c) or "").strip():
            return Response({"error": "Email body is required."}, status=400)

        if (
            addon.stripe_subscription_id
            and (addon.current_period_start is None or addon.current_period_end is None)
        ):
            sync_addon_subscription_from_stripe(
                addon.stripe_subscription_id,
                addon_type=ADDON_TYPE_EMAIL_MARKETING,
            )
            addon.refresh_from_db()

        tier = price_id_to_tier(addon.stripe_price_id)
        at = (c.audience_type or "all_contacts").strip()
        flt = c.audience_filter or {}
        tier_err = audience_tier_error(tier, at, flt)
        if tier_err:
            return Response({"error": tier_err}, status=403)
        qs, _ = build_contact_queryset(business, at, flt)
        n = qs.count()
        try:
            assert_can_send(business, addon, n)
        except MarketingQuotaExceeded as e:
            return Response(
                {"error": str(e), "used": e.used, "limit": e.limit},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        c.status = "sending"
        c.error_message = ""
        c.save(update_fields=["status", "error_message"])
        send_business_marketing_campaign_task.delay(str(c.id))
        return Response({"status": "queued", "recipient_count": n})


class MarketingCampaignTestSendView(APIView):
    """Sends one preview to the authenticated user; does not count toward marketing quota."""

    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def post(self, request, campaign_id):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        c = get_object_or_404(BusinessEmailCampaign, id=campaign_id, business=business)
        tier = price_id_to_tier(addon.stripe_price_id)
        to = (request.user.email or "").strip()
        if not to:
            return Response({"error": "No email on account."}, status=400)
        from quickstart.models import Contact

        contact = Contact.objects.filter(business=business, user=request.user).first()
        if not contact:
            contact = Contact(
                business=business,
                first_name=getattr(request.user, "first_name", "") or "Friend",
                last_name=getattr(request.user, "last_name", "") or "",
                email=to,
            )
        subject = _merge_fields(c.subject or "Test", contact, business)
        body = _merge_fields(resolve_campaign_html_body(c), contact, business)
        if contact.id:
            body += _footer_html(business, contact)
        else:
            body += "<p><i>Test send — unsubscribe links apply to real contacts only.</i></p>"
        from_header, from_email, reply_to = _resolve_from_header(
            business, c.sender_profile, tier
        )
        resend.api_key = settings.RESEND_API_KEY
        try:
            rate_limiter.wait_if_needed()
            params = {
                "from": from_header,
                "to": [to],
                "subject": subject[:1900],
                "html": body,
            }
            if reply_to:
                params["reply_to"] = reply_to
            resend.Emails.send(params)
        except Exception as e:
            logger.exception("test send: %s", e)
            return Response({"error": str(e)}, status=502)
        return Response({"status": "sent", "to": to})


class MarketingEmailUnsubscribeView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token):
        try:
            raw = _UNSUB_SIGNER.unsign(token)
        except BadSignature:
            return HttpResponsePlain("Invalid or expired unsubscribe link.", status=400)
        parts = raw.split(":", 1)
        if len(parts) != 2:
            return HttpResponsePlain("Invalid link.", status=400)
        bid_s, cid_s = parts
        from quickstart.models import Contact

        try:
            contact = Contact.objects.get(id=cid_s, business_id=int(bid_s))
        except (Contact.DoesNotExist, ValueError):
            return HttpResponsePlain("Contact not found.", status=404)
        contact.marketing_unsubscribed = True
        contact.marketing_unsubscribed_at = timezone.now()
        contact.save(update_fields=["marketing_unsubscribed", "marketing_unsubscribed_at"])
        return HttpResponsePlain(
            f"You have been unsubscribed from marketing emails from {contact.business.businessName}."
        )


def HttpResponsePlain(text, status=200):
    from django.http import HttpResponse

    return HttpResponse(text, content_type="text/plain; charset=utf-8", status=status)
