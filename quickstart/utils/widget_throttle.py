"""
Rate throttle for public widget API. Keys by X-Business-ID (or IP) so limits
are per business/embed, not global.
"""
from rest_framework.throttling import SimpleRateThrottle


class WidgetRateThrottle(SimpleRateThrottle):
    scope = "widget"

    def get_cache_key(self, request, view):
        business_id = request.headers.get("X-Business-ID")
        if business_id:
            return self.cache_format % {"scope": self.scope, "ident": business_id}
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident or "unknown"}
