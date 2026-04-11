"""
Server-side preview for transactional email branding (sample placeholder data).
"""

import logging

from django.db.models import Q
from django.utils.html import escape
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from quickstart.utils.permissions import CanManageOwnClasses
from quickstart.utils.email_branding_html import (
    render_custom_html_template,
    wrap_email_html_fragment_if_needed,
)
from quickstart.utils.email_branding_placeholders import build_sample_placeholder_map

logger = logging.getLogger(__name__)


class EmailBrandingPreviewView(APIView):
    """
    POST body: {
      "branding": { ... full or partial branding JSON including mode, custom_html },
      "email_type": "booking_confirmation" | ...
    }
    Returns { "html": "..." } with sample data substituted (HTML mode only useful when custom_html set).
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get_business(self, user):
        from quickstart.models import BusinessInfo

        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            from rest_framework.exceptions import NotFound

            raise NotFound("You are not a member of any business.")
        return business

    def post(self, request, *args, **kwargs):
        email_type = (request.data.get("email_type") or "").strip()
        branding = request.data.get("branding")
        if not email_type:
            return Response(
                {"detail": "email_type is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(branding, dict):
            return Response(
                {"detail": "branding must be an object."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        self.get_business(request.user)  # ensure permission

        sample = build_sample_placeholder_map(email_type)
        mode = branding.get("mode") or "builder"
        if mode == "html":
            custom = branding.get("custom_html") or {}
            raw = custom.get(email_type)
            if not raw or not str(raw).strip():
                return Response(
                    {
                        "html": "",
                        "detail": "No custom HTML for this email type.",
                    },
                    status=status.HTTP_200_OK,
                )
            ph_map = {k: str(v) for k, v in sample.items()}
            body = render_custom_html_template(str(raw), ph_map)
            wrapped = wrap_email_html_fragment_if_needed(
                body, title="Email preview"
            )
            return Response({"html": wrapped}, status=status.HTTP_200_OK)

        # Builder mode: return minimal preview shell with branding colors (client still does main preview)
        primary = (branding.get("primary_color") or "#f81e3e").strip()
        bg = (branding.get("background_color") or "#F5F5F7").strip()
        logo = (branding.get("logo_url") or "").strip()
        footer = (branding.get("footer_text") or "").strip()
        msg = (branding.get("confirmation_message") or "").strip()
        logo_html = ""
        if logo:
            w = branding.get("logo_max_width") or 160
            h = branding.get("logo_max_height") or 60
            try:
                w, h = int(w), int(h)
            except (TypeError, ValueError):
                w, h = 160, 60
            logo_html = f'<div style="text-align:center;margin-bottom:24px;"><img src="{logo}" alt="" style="max-width:{w}px;max-height:{h}px"/></div>'
        btn = escape(str(branding.get("button_text") or "View details"))
        inner = f"""
        {logo_html}
        <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;padding:32px;border:1px solid #E5E7EB;">
          <p style="color:#1D1D1F;font-size:18px;margin:0 0 8px 0;"><strong>Sample booking</strong></p>
          <p style="color:#484848;margin:0 0 16px 0;">{escape(sample.get("class_title", ""))} · {escape(sample.get("booking_date", ""))} at {escape(sample.get("booking_time", ""))}</p>
          {f'<p style="color:#484848;font-size:14px;">{escape(msg)}</p>' if msg else ''}
          <a href="#" style="display:inline-block;background:{escape(primary)};color:#fff;padding:12px 28px;border-radius:999px;text-decoration:none;font-weight:600;">{btn}</a>
        </div>
        {f'<p style="text-align:center;color:#86868B;font-size:12px;margin-top:24px;">{escape(footer)}</p>' if footer else ''}
        """
        html = wrap_email_html_fragment_if_needed(
            f'<div style="background:{bg};padding:24px;">{inner}</div>',
            title="Branding preview",
        )
        return Response({"html": html}, status=status.HTTP_200_OK)
