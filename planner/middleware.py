from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.conf import settings
from django.shortcuts import redirect

from .feature_flags import feature_enabled
from .privacy import (
    PRIVACY_MODE_QUERY_PARAM_VALUE_OFF,
    PRIVACY_MODE_QUERY_PARAM_VALUE_ON,
    PRIVACY_MODE_SESSION_KEY,
    reset_privacy_mode,
    set_privacy_mode,
)


class RequireLoginMiddleware:
    exempt_prefixes = ("/health/", "/static/", "/login/", "/logout/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self.should_require_login(request):
            return redirect_to_login(request.get_full_path(), login_url=settings.LOGIN_URL)
        return self.get_response(request)

    def should_require_login(self, request):
        if not settings.LIF_REQUIRE_LOGIN:
            return False
        if request.user.is_authenticated:
            return False
        return not request.path.startswith(self.exempt_prefixes)


class ReadOnlyModeMiddleware:
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self.should_block(request):
            messages.error(request, "Read-only mode is active. No changes were saved.")
            return redirect(request.META.get("HTTP_REFERER") or "planner:dashboard")
        return self.get_response(request)

    def should_block(self, request):
        if request.method not in self.write_methods:
            return False
        if not feature_enabled("read_only_mode"):
            return False
        return request.path.startswith("/") and not request.path.startswith("/admin/")


class PrivacyModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        enabled = self._privacy_enabled(request)
        token = set_privacy_mode(enabled)
        try:
            return self.get_response(request)
        finally:
            reset_privacy_mode(token)

    @staticmethod
    def _privacy_enabled(request):
        query_value = request.GET.get(settings.LIF_PRIVACY_QUERY_PARAM)
        if query_value == PRIVACY_MODE_QUERY_PARAM_VALUE_ON:
            return True
        if query_value == PRIVACY_MODE_QUERY_PARAM_VALUE_OFF:
            return False
        return request.session.get(PRIVACY_MODE_SESSION_KEY, False)
