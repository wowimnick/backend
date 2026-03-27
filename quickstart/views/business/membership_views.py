# quickstart/views/business/membership_views.py
"""
Business dashboard API for membership products and customer members.
"""
import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from quickstart.models import (
    BusinessInfo,
    ClassesMain,
    Contact,
    CustomerMembership,
    MembershipCreditLedger,
    MembershipPayment,
    MembershipProduct,
)
from quickstart.services.membership_service import (
    approve_membership,
    create_stripe_price,
    get_credits_remaining,
)
from quickstart.services.membership_sync import sync_customer_membership_from_stripe
from quickstart.utils.permissions import CanManageOwnClasses

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def _get_business(user):
    """Get the business for the current user (owner or accepted staff)."""
    business = BusinessInfo.objects.filter(
        Q(owner=user) | Q(staff_members__user=user, staff_members__status="accepted")
    ).first()
    if not business:
        from rest_framework.exceptions import NotFound
        raise NotFound("You are not a member of any business.")
    return business


def _normalize_widget_button_config(raw):
    """Sanitize per-product embed button options for Sell Memberships snippets."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    oc = raw.get("open_class_id")
    if oc is not None and oc != "":
        try:
            out["open_class_id"] = int(oc)
        except (TypeError, ValueError):
            out["open_class_id"] = None
    else:
        out["open_class_id"] = None
    bl = raw.get("button_label")
    if isinstance(bl, str) and bl.strip():
        out["button_label"] = bl.strip()[:120]
    for key, maxlen in (
        ("button_background", 32),
        ("button_text_color", 32),
    ):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()[:maxlen]
    preset = raw.get("button_radius_preset")
    if isinstance(preset, str) and preset.strip().lower() in (
        "none",
        "small",
        "medium",
        "large",
    ):
        out["button_radius_preset"] = preset.strip().lower()
    else:
        br = raw.get("button_radius_px")
        if br is not None and br != "":
            try:
                n = int(br)
                out["button_radius_px"] = max(0, min(48, n))
            except (TypeError, ValueError):
                pass
    return out


def _normalize_widget_features(raw):
    """Sanitize per-product widget feature flags; must be a JSON object (default {})."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {}
    return dict(raw)


def _validate_signup_fields(signup_fields):
    """Validate signup_fields list; return (True, None) or (False, error_message)."""
    if signup_fields is None:
        return True, None
    if not isinstance(signup_fields, list):
        return False, "signup_fields must be a list"
    allowed_types = {"text", "textarea", "select", "checkbox"}
    keys_seen = set()
    for i, field in enumerate(signup_fields):
        if not isinstance(field, dict):
            return False, f"signup_fields[{i}] must be an object"
        key = field.get("key")
        label = field.get("label")
        if not key or not isinstance(key, str) or not key.strip():
            return False, f"signup_fields[{i}] must have a non-empty key"
        if not label or not isinstance(label, str) or not label.strip():
            return False, f"signup_fields[{i}] must have a non-empty label"
        if key.strip() in keys_seen:
            return False, f"signup_fields: duplicate key '{key.strip()}'"
        keys_seen.add(key.strip())
        ftype = field.get("type", "text")
        if ftype not in allowed_types:
            return False, f"signup_fields[{i}].type must be one of: {', '.join(sorted(allowed_types))}"
    return True, None


def _product_to_dict(product):
    """Serialize MembershipProduct for API response."""
    return {
        "id": str(product.id),
        "name": product.name,
        "badge_text": getattr(product, "badge_text", "") or "",
        "description": product.description or "",
        "price": str(product.price),
        "currency": product.currency,
        "billing_interval": product.billing_interval,
        "access_type": product.access_type,
        "credit_allowance": product.credit_allowance,
        "credit_unit": product.credit_unit or "",
        "applicable_class_ids": list(
            product.applicable_classes.values_list("classId", flat=True)
        ),
        "is_active": product.is_active,
        "requires_approval": product.requires_approval,
        "confirmation_message": getattr(product, "confirmation_message", "") or "",
        "welcome_url": getattr(product, "welcome_url", "") or "",
        "application_instructions": getattr(product, "application_instructions", "") or "",
        "signup_fields": getattr(product, "signup_fields", None) or [],
        "max_members": getattr(product, "max_members", None),
        "trial_period_days": getattr(product, "trial_period_days", None),
        "widget_button_config": getattr(product, "widget_button_config", None) or {},
        "widget_features": getattr(product, "widget_features", None) or {},
        "widget_cta_label": getattr(product, "widget_cta_label", "") or "",
        "stripe_price_id": product.stripe_price_id,
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
        "active_members_count": getattr(product, "_active_members_count", 0),
    }


def _member_to_dict(membership):
    """Serialize CustomerMembership for list/detail."""
    contact = membership.contact
    product = membership.product
    name = ""
    email = ""
    if contact:
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "—"
        email = contact.email or ""
    elif membership.user:
        name = getattr(membership.user, "first_name", "") or getattr(membership.user, "email", "")
        email = getattr(membership.user, "email", "") or ""

    credits_remaining = None
    credit_allowance = None
    credit_unit = ""
    if product.access_type == "credits" and product.credit_allowance:
        credits_remaining = get_credits_remaining(membership)
        credit_allowance = product.credit_allowance
        credit_unit = product.credit_unit or ""

    return {
        "id": str(membership.id),
        "product_id": str(membership.product_id),
        "product_name": membership.product.name,
        "contact_id": str(membership.contact_id) if membership.contact_id else None,
        "name": name,
        "email": email,
        "status": membership.status,
        "current_period_start": membership.current_period_start.isoformat() if membership.current_period_start else None,
        "current_period_end": membership.current_period_end.isoformat() if membership.current_period_end else None,
        "cancel_at_period_end": membership.cancel_at_period_end,
        "source": membership.source,
        "notes": membership.notes or "",
        "custom_data": getattr(membership, "custom_data", None) or {},
        "credits_remaining": credits_remaining,
        "credit_allowance": credit_allowance,
        "credit_unit": credit_unit,
        "created_at": membership.created_at.isoformat() if membership.created_at else None,
    }


class MembershipProductListCreateView(APIView):
    """GET: List membership products for the business. POST: Create a new product."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request):
        business = _get_business(request.user)
        products = (
            MembershipProduct.objects.filter(business=business)
            .annotate(_active_members_count=Count("memberships", filter=Q(memberships__status__in=["active", "trialing"])))
            .order_by("-created_at")
        )
        return Response([_product_to_dict(p) for p in products], status=status.HTTP_200_OK)

    def post(self, request):
        business = _get_business(request.user)
        data = request.data
        name = (data.get("name") or "").strip()
        if not name:
            return Response(
                {"error": "name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        price = data.get("price")
        if price is None:
            return Response(
                {"error": "price is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            price = float(price)
            if price < 0:
                raise ValueError("price must be >= 0")
        except (TypeError, ValueError):
            return Response(
                {"error": "price must be a non-negative number"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        signup_fields = data.get("signup_fields")
        ok, err = _validate_signup_fields(signup_fields)
        if not ok:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        wf = data.get("widget_features")
        if wf is not None and not isinstance(wf, dict):
            return Response(
                {"error": "widget_features must be an object"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        product = MembershipProduct.objects.create(
            business=business,
            name=name,
            badge_text=(data.get("badge_text") or "").strip()[:100],
            description=(data.get("description") or "").strip(),
            price=Decimal(str(price)),
            currency=(data.get("currency") or "CAD").strip().upper()[:3],
            billing_interval=(data.get("billing_interval") or "month").strip().lower(),
            access_type=(data.get("access_type") or "unlimited").strip().lower(),
            credit_allowance=data.get("credit_allowance"),
            credit_unit=(data.get("credit_unit") or "").strip()[:100],
            is_active=data.get("is_active", True),
            requires_approval=data.get("requires_approval", False),
            confirmation_message=(data.get("confirmation_message") or "").strip(),
            welcome_url=(data.get("welcome_url") or "").strip()[:500],
            application_instructions=(data.get("application_instructions") or "").strip(),
            signup_fields=signup_fields if isinstance(signup_fields, list) else [],
            max_members=data.get("max_members") if data.get("max_members") is not None else None,
            trial_period_days=data.get("trial_period_days") if data.get("trial_period_days") is not None else None,
            widget_button_config=_normalize_widget_button_config(data.get("widget_button_config")),
            widget_features=_normalize_widget_features(wf),
            widget_cta_label=(data.get("widget_cta_label") or "").strip()[:200],
        )
        if data.get("applicable_class_ids"):
            try:
                class_ids = [int(x) for x in data["applicable_class_ids"]]
                product.applicable_classes.set(
                    ClassesMain.objects.filter(
                        businessId=business, classId__in=class_ids, status="active"
                    )
                )
            except (TypeError, ValueError):
                pass
        return Response(_product_to_dict(product), status=status.HTTP_201_CREATED)


class MembershipProductDetailView(APIView):
    """GET, PATCH, DELETE a single membership product."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def _get_product(self, request, product_id):
        business = _get_business(request.user)
        return get_object_or_404(
            MembershipProduct.objects.annotate(
                _active_members_count=Count("memberships", filter=Q(memberships__status__in=["active", "trialing"]))
            ),
            id=product_id,
            business=business,
        )

    def get(self, request, product_id):
        product = self._get_product(request, product_id)
        return Response(_product_to_dict(product), status=status.HTTP_200_OK)

    def patch(self, request, product_id):
        product = self._get_product(request, product_id)
        data = request.data
        if "name" in data and data["name"] is not None:
            product.name = (data["name"] or "").strip() or product.name
        if "badge_text" in data:
            product.badge_text = (data.get("badge_text") or "").strip()[:100]
        if "description" in data:
            product.description = (data["description"] or "").strip()
        if "price" in data and data["price"] is not None:
            try:
                product.price = Decimal(str(data["price"]))
            except (TypeError, ValueError):
                pass
        if "currency" in data and data["currency"]:
            product.currency = (data["currency"] or "").strip().upper()[:3]
        if "billing_interval" in data and data["billing_interval"]:
            product.billing_interval = (data["billing_interval"] or "month").strip().lower()
        if "access_type" in data and data["access_type"]:
            product.access_type = (data["access_type"] or "unlimited").strip().lower()
        if "credit_allowance" in data:
            product.credit_allowance = data["credit_allowance"]
        if "credit_unit" in data:
            product.credit_unit = (data["credit_unit"] or "").strip()[:100]
        if "is_active" in data:
            product.is_active = bool(data["is_active"])
        if "requires_approval" in data:
            product.requires_approval = bool(data["requires_approval"])
        if "confirmation_message" in data:
            product.confirmation_message = (data.get("confirmation_message") or "").strip()
        if "welcome_url" in data:
            product.welcome_url = (data.get("welcome_url") or "").strip()[:500]
        if "application_instructions" in data:
            product.application_instructions = (data.get("application_instructions") or "").strip()
        if "signup_fields" in data:
            ok, err = _validate_signup_fields(data["signup_fields"])
            if not ok:
                return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
            product.signup_fields = data["signup_fields"] if isinstance(data["signup_fields"], list) else []
        if "max_members" in data:
            product.max_members = data["max_members"] if data["max_members"] is not None else None
        if "trial_period_days" in data:
            product.trial_period_days = data["trial_period_days"] if data["trial_period_days"] is not None else None
        if "widget_button_config" in data:
            product.widget_button_config = _normalize_widget_button_config(
                data.get("widget_button_config")
            )
        if "widget_features" in data:
            wfp = data.get("widget_features")
            if wfp is not None and not isinstance(wfp, dict):
                return Response(
                    {"error": "widget_features must be an object"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            product.widget_features = _normalize_widget_features(wfp)
        if "widget_cta_label" in data:
            product.widget_cta_label = (data.get("widget_cta_label") or "").strip()[:200]
        if "applicable_class_ids" in data:
            business = _get_business(request.user)
            if data["applicable_class_ids"] is None or data["applicable_class_ids"] == []:
                product.applicable_classes.clear()
            else:
                try:
                    class_ids = [int(x) for x in data["applicable_class_ids"]]
                    product.applicable_classes.set(
                        ClassesMain.objects.filter(
                            businessId=business, classId__in=class_ids, status="active"
                        )
                    )
                except (TypeError, ValueError):
                    pass
        product.save()
        product._active_members_count = getattr(product, "_active_members_count", 0) or product.memberships.filter(status__in=["active", "trialing"]).count()
        return Response(_product_to_dict(product), status=status.HTTP_200_OK)

    def delete(self, request, product_id):
        product = self._get_product(request, product_id)
        active = product.memberships.filter(status__in=["active", "trialing"]).exists()
        if active:
            return Response(
                {"error": "Cannot delete a product with active members. Cancel memberships first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        product.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MembershipProductSyncStripeView(APIView):
    """POST: Create or update Stripe Price for this product."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, product_id):
        business = _get_business(request.user)
        product = get_object_or_404(MembershipProduct, id=product_id, business=business)
        try:
            create_stripe_price(product)
        except Exception as e:
            logger.exception("membership product sync-stripe failed: %s", e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"stripe_price_id": product.stripe_price_id}, status=status.HTTP_200_OK)


class MemberListView(APIView):
    """GET: List customer memberships with optional filters."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request):
        business = _get_business(request.user)
        qs = CustomerMembership.objects.filter(product__business=business).select_related(
            "product", "contact", "user"
        ).order_by("-created_at")
        # Dashboard: never list incomplete/expired Stripe states — not actionable for the business.
        qs = qs.exclude(status__in=["incomplete", "expired"])

        product_id = request.query_params.get("product_id")
        if product_id:
            try:
                qs = qs.filter(product_id=product_id)
            except (ValueError, TypeError):
                pass
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.lower())
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(contact__first_name__icontains=search)
                | Q(contact__last_name__icontains=search)
                | Q(contact__email__icontains=search)
                | Q(user__email__icontains=search)
            )
        page = int(request.query_params.get("page", 1) or 1)
        page_size = min(int(request.query_params.get("page_size", 20) or 20), 100)
        offset = (page - 1) * page_size
        total = qs.count()
        items = list(qs[offset : offset + page_size])
        # Backfill billing period from Stripe when missing (list view does not load detail).
        for m in items:
            if m.stripe_subscription_id and (
                m.current_period_start is None or m.current_period_end is None
            ):
                synced, sync_err = sync_customer_membership_from_stripe(
                    m.stripe_subscription_id,
                    subscription_obj=None,
                    invoice_obj=None,
                )
                if sync_err:
                    logger.debug(
                        "MemberListView: stripe period sync skipped for %s: %s",
                        m.id,
                        sync_err,
                    )
                if synced is not None:
                    m.refresh_from_db()
        return Response(
            {
                "results": [_member_to_dict(m) for m in items],
                "count": total,
                "page": page,
                "page_size": page_size,
            },
            status=status.HTTP_200_OK,
        )


class MemberDetailView(APIView):
    """GET: Detail for a customer membership (with ledger and payment history)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, member_id):
        business = _get_business(request.user)
        membership = get_object_or_404(
            CustomerMembership.objects.select_related("product", "contact", "user"),
            id=member_id,
            product__business=business,
        )
        # Fill billing period from Stripe when missing (e.g. before webhooks or backfill).
        if membership.stripe_subscription_id and (
            membership.current_period_start is None or membership.current_period_end is None
        ):
            synced, sync_err = sync_customer_membership_from_stripe(
                membership.stripe_subscription_id,
                subscription_obj=None,
                invoice_obj=None,
            )
            if sync_err:
                logger.warning(
                    "MemberDetailView: could not sync Stripe periods for membership %s: %s",
                    membership.id,
                    sync_err,
                )
            if synced is not None:
                membership.refresh_from_db()
        ledger = list(
            MembershipCreditLedger.objects.filter(membership=membership).order_by("-created_at")[:100]
        )
        payments = list(
            MembershipPayment.objects.filter(membership=membership).order_by("-created_at")[:50]
        )
        data = _member_to_dict(membership)
        data["ledger"] = [
            {
                "id": str(e.id),
                "period_start": e.period_start.isoformat() if e.period_start else None,
                "credits_used": e.credits_used,
                "action": e.action,
                "booking_id": e.booking_id,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in ledger
        ]
        data["payments"] = [
            {
                "id": str(p.id),
                "amount": str(p.amount),
                "platform_fee_amount": str(p.platform_fee_amount),
                "net_payout_amount": str(p.net_payout_amount),
                "status": p.status,
                "period_start": p.period_start.isoformat() if p.period_start else None,
                "period_end": p.period_end.isoformat() if p.period_end else None,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in payments
        ]
        return Response(data, status=status.HTTP_200_OK)


class MemberCancelView(APIView):
    """POST: Set cancel_at_period_end or cancel immediately (for Stripe-backed only)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, member_id):
        business = _get_business(request.user)
        membership = get_object_or_404(
            CustomerMembership,
            id=member_id,
            product__business=business,
        )
        immediate = request.data.get("immediate", False)
        if membership.stripe_subscription_id:
            try:
                if immediate:
                    stripe.Subscription.cancel(membership.stripe_subscription_id)
                    membership.status = "canceled"
                    membership.save(update_fields=["status"])
                else:
                    stripe.Subscription.modify(
                        membership.stripe_subscription_id,
                        cancel_at_period_end=True,
                    )
                    membership.cancel_at_period_end = True
                    membership.save(update_fields=["cancel_at_period_end"])
            except stripe.StripeError as e:
                return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        else:
            membership.status = "canceled"
            membership.save(update_fields=["status"])
        return Response(_member_to_dict(membership), status=status.HTTP_200_OK)


class MemberManualAddView(APIView):
    """POST: Create a CustomerMembership with source=manual (no Stripe)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request):
        business = _get_business(request.user)
        data = request.data
        product_id = data.get("product_id")
        if not product_id:
            return Response(
                {"error": "product_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        product = get_object_or_404(MembershipProduct, id=product_id, business=business)
        contact_id = data.get("contact_id")
        email = (data.get("email") or "").strip()
        first_name = (data.get("first_name") or "").strip()
        last_name = (data.get("last_name") or "").strip()
        if contact_id:
            contact = get_object_or_404(Contact, id=contact_id, business=business)
        elif email:
            contact, _ = Contact.objects.get_or_create(
                business=business,
                email__iexact=email,
                defaults={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "source": "manual_entry",
                },
            )
            if contact.first_name != first_name or contact.last_name != last_name:
                contact.first_name = first_name or contact.first_name
                contact.last_name = last_name or contact.last_name
                contact.save(update_fields=["first_name", "last_name"])
        else:
            return Response(
                {"error": "contact_id or email is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from django.utils import timezone
        from datetime import timedelta
        start = timezone.now()
        end = start + timedelta(days=30) if product.billing_interval == "month" else start + timedelta(days=365)
        membership = CustomerMembership.objects.create(
            product=product,
            contact=contact,
            user=None,
            stripe_subscription_id=None,
            stripe_customer_id=None,
            status="active",
            current_period_start=start,
            current_period_end=end,
            cancel_at_period_end=False,
            source="manual",
            notes=(data.get("notes") or "").strip(),
            custom_data=data.get("custom_data") if isinstance(data.get("custom_data"), dict) else {},
        )
        return Response(_member_to_dict(membership), status=status.HTTP_201_CREATED)


class MemberApproveView(APIView):
    """POST: Approve a pending_approval membership; creates Stripe subscription and emails payment link."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, pk):
        business = _get_business(request.user)
        membership = get_object_or_404(
            CustomerMembership.objects.select_related("product", "contact"),
            id=pk,
            product__business=business,
        )
        if membership.status != "pending_approval":
            return Response(
                {"error": "Only memberships with status 'pending_approval' can be approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            approve_membership(membership)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception("MemberApproveView failed: %s", e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        membership.refresh_from_db()
        return Response(_member_to_dict(membership), status=status.HTTP_200_OK)


class MemberDeclineView(APIView):
    """POST: Decline a pending_approval membership; set status to canceled."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, pk):
        business = _get_business(request.user)
        membership = get_object_or_404(
            CustomerMembership,
            id=pk,
            product__business=business,
        )
        if membership.status != "pending_approval":
            return Response(
                {"error": "Only memberships with status 'pending_approval' can be declined."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        membership.status = "canceled"
        membership.save(update_fields=["status", "updated_at"])
        return Response(_member_to_dict(membership), status=status.HTTP_200_OK)
