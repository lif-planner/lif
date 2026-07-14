from django.conf import settings
from django.contrib.staticfiles import finders
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpResponseRedirect, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import check_for_language

from .version import version_context


def migrations_current():
    executor = MigrationExecutor(connection)
    return not executor.migration_plan(executor.loader.graph.leaf_nodes())


def health(request):
    checks = {}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = True
    except Exception:
        checks["database"] = False

    try:
        checks["migrations"] = migrations_current()
    except Exception:
        checks["migrations"] = False

    checks["staticfiles"] = bool(finders.find("planner/app.css"))

    healthy = all(checks.values())
    payload = {
        "status": "ok" if healthy else "degraded",
        "debug": settings.DEBUG,
        "checks": checks,
        **version_context(),
    }
    return JsonResponse(payload, status=200 if healthy else 503)


def set_language_local(request):
    language = request.POST.get("language", "")
    target = _safe_language_redirect_target(request)
    response = HttpResponseRedirect(target)

    if check_for_language(language):
        _set_language_cookie(response, settings.LANGUAGE_COOKIE_NAME, language)
        _set_language_cookie(response, settings.LIF_LANGUAGE_COOKIE_NAME, language)
        if hasattr(request, "session"):
            request.session[settings.LIF_LANGUAGE_COOKIE_NAME] = language
            request.session[settings.LANGUAGE_COOKIE_NAME] = language

    return response


def _set_language_cookie(response, name, language):
    response.set_cookie(
        name,
        language,
        max_age=settings.LANGUAGE_COOKIE_AGE,
        path=settings.LANGUAGE_COOKIE_PATH,
        domain=settings.LANGUAGE_COOKIE_DOMAIN,
        secure=settings.LANGUAGE_COOKIE_SECURE,
        httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
        samesite=settings.LANGUAGE_COOKIE_SAMESITE,
    )


def _safe_language_redirect_target(request):
    fallback = _with_script_name(request, "/")
    target = request.POST.get("next") or request.GET.get("next") or fallback
    allowed = url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    )
    if not allowed:
        return fallback

    script_name = request.META.get("SCRIPT_NAME", "").rstrip("/")
    if script_name and not target.startswith(f"{script_name}/") and target != script_name:
        return fallback
    return target


def _with_script_name(request, path):
    script_name = request.META.get("SCRIPT_NAME", "").rstrip("/")
    if not script_name:
        return path if path.startswith("/") else f"/{path}"
    if not path.startswith("/"):
        path = f"/{path}"
    if path == script_name or path.startswith(f"{script_name}/"):
        return path
    return f"{script_name}{path}"
