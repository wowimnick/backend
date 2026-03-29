"""Resend domain API (platform API key)."""
import logging

import resend
from django.conf import settings

logger = logging.getLogger(__name__)


def _client():
    resend.api_key = settings.RESEND_API_KEY


def resend_create_domain(name: str):
    _client()
    return resend.Domains.create({"name": name.strip().lower()})


def resend_get_domain(domain_id: str):
    _client()
    return resend.Domains.get(domain_id)


def resend_verify_domain(domain_id: str):
    _client()
    return resend.Domains.verify(domain_id)


def resend_remove_domain(domain_id: str):
    _client()
    return resend.Domains.remove(domain_id)
