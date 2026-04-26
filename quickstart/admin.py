from django.contrib import admin

from quickstart.models import CorporateInquiry


@admin.register(CorporateInquiry)
class CorporateInquiryAdmin(admin.ModelAdmin):
    list_display = (
        "company_name",
        "contact_name",
        "email",
        "company_size",
        "created_at",
    )
    list_filter = ("company_size", "created_at")
    search_fields = ("company_name", "contact_name", "email", "phone")
    readonly_fields = ("id", "created_at", "meta")
    ordering = ("-created_at",)
