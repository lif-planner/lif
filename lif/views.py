from django.conf import settings
from django.contrib.staticfiles import finders
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse

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
