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
    create_stripe_price,
    get_credits_remaining,
)
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


def _product_to_dict(product):
    """Serialize MembershipProduct for API response."""
    return {
        "id": str(product.id),
        "name": product.name,
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
    if product.access_type == "credits" and product.credit_allowance and membership.current_period_start:
        credits_remaining = get_credits_remaining(membership)

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
        "credits_remaining": credits_remaining,
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
        product = MembershipProduct.objects.create(
            business=business,
            name=name,
            description=(data.get("description") or "").strip(),
            price=Decimal(str(price)),
            currency=(data.get("currency") or "CAD").strip().upper()[:3],
            billing_interval=(data.get("billing_interval") or "month").strip().lower(),
            access_type=(data.get("access_type") or "unlimited").strip().lower(),
            credit_allowance=data.get("credit_allowance"),
            credit_unit=(data.get("credit_unit") or "").strip()[:100],
            is_active=data.get("is_active", True),
            requires_approval=data.get("requires_approval", False),
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
                "period_start": str(e.period_start),
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


class MemberPauseView(APIView):
    """POST: Pause a membership (manual status update)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, member_id):
        business = _get_business(request.user)
        membership = get_object_or_404(
            CustomerMembership,
            id=member_id,
            product__business=business,
        )
        membership.status = "paused"
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
        )
        return Response(_member_to_dict(membership), status=status.HTTP_201_CREATED)
