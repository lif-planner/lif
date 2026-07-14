import re

from django.conf import settings
from django.urls import get_script_prefix, set_script_prefix
from django.utils.translation import check_for_language


INGRESS_PATH_RE = re.compile(r"^(/api/hassio_ingress/[^/]+)(?:/|$)")


class HomeAssistantIngressMiddleware:
    """Support Home Assistant Ingress path prefixes.

    Home Assistant forwards add-on UI requests through a dynamic base path and
    sends that base path as X-Ingress-Path. Django needs the prefix removed for
    URL resolving, but URL generation still needs to include it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ingress_path = self._normalized_ingress_path(request)
        if not ingress_path:
            return self.get_response(request)

        original_prefix = get_script_prefix()
        request.META["SCRIPT_NAME"] = ingress_path
        if self._should_bypass_csrf(request):
            request._dont_enforce_csrf_checks = True
        self._strip_ingress_path(request, ingress_path)
        set_script_prefix(f"{ingress_path}/")
        try:
            return self.get_response(request)
        finally:
            set_script_prefix(original_prefix)

    @staticmethod
    def _normalized_ingress_path(request):
        ingress_path = request.headers.get("X-Ingress-Path", "").strip()
        if not ingress_path:
            match = INGRESS_PATH_RE.match(request.META.get("PATH_INFO", ""))
            ingress_path = match.group(1) if match else ""
        if not ingress_path:
            return ""
        if not ingress_path.startswith("/"):
            ingress_path = f"/{ingress_path}"
        return ingress_path.rstrip("/")

    @staticmethod
    def _strip_ingress_path(request, ingress_path):
        path_info = request.META.get("PATH_INFO", "")
        if path_info == ingress_path:
            stripped = "/"
        elif path_info.startswith(f"{ingress_path}/"):
            stripped = path_info[len(ingress_path) :] or "/"
        else:
            stripped = path_info

        request.META["PATH_INFO"] = stripped
        request.path_info = stripped
        request.path = f"{ingress_path}{stripped}"

    @staticmethod
    def _should_bypass_csrf(request):
        if not settings.LIF_HOME_ASSISTANT_ADDON:
            return False
        return True


class PersistentLanguageMiddleware:
    """Restore LiF's explicit language choice before Django selects a locale."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        language = self._preferred_language(request)
        if language:
            request.COOKIES[settings.LANGUAGE_COOKIE_NAME] = language
        return self.get_response(request)

    @staticmethod
    def _preferred_language(request):
        candidates = [
            request.GET.get(settings.LIF_LANGUAGE_QUERY_PARAM),
            request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME),
            request.COOKIES.get(settings.LIF_LANGUAGE_COOKIE_NAME),
        ]
        if hasattr(request, "session"):
            candidates.append(request.session.get(settings.LIF_LANGUAGE_COOKIE_NAME))
            candidates.append(request.session.get(settings.LANGUAGE_COOKIE_NAME))

        for language in candidates:
            if language and check_for_language(language):
                return language
        return ""
