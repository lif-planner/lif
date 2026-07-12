from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from planner.feature_flags import FEATURE_FLAG_DEFINITIONS, feature_flag_map
from planner.models import FeatureFlag


class Command(BaseCommand):
    help = "Check local production-readiness settings and operational safety basics."

    def handle(self, *args, **options):
        warnings = []

        if settings.DEBUG:
            warnings.append("DJANGO_DEBUG is enabled.")
        if settings.SECRET_KEY == "django-insecure-local-dev-only":
            warnings.append("DJANGO_SECRET_KEY is using the local development default.")
        if not settings.ALLOWED_HOSTS:
            warnings.append("DJANGO_ALLOWED_HOSTS is empty.")
        if not settings.LIF_REQUIRE_LOGIN:
            warnings.append("LIF_REQUIRE_LOGIN is disabled; planner data is visible without app login.")

        backup_dir = Path(settings.BACKUP_DIR)
        if not backup_dir.exists():
            warnings.append(f"Backup directory does not exist yet: {backup_dir}")

        static_root = Path(settings.STATIC_ROOT)
        if not settings.DEBUG and not static_root.exists():
            warnings.append(f"Static file directory does not exist yet: {static_root}. Run collectstatic.")

        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if pending:
            warnings.append(f"{len(pending)} migration(s) are pending.")

        missing_flags = set(FEATURE_FLAG_DEFINITIONS) - set(FeatureFlag.objects.values_list("key", flat=True))
        if missing_flags:
            warnings.append(f"Missing feature flag rows: {', '.join(sorted(missing_flags))}")

        self.stdout.write("Feature flags:")
        for key, enabled in sorted(feature_flag_map().items()):
            self.stdout.write(f"  {key}: {'on' if enabled else 'off'}")

        if warnings:
            self.stdout.write(self.style.WARNING("Production readiness warnings:"))
            for warning in warnings:
                self.stdout.write(self.style.WARNING(f"  - {warning}"))
            return

        self.stdout.write(self.style.SUCCESS("Production readiness checks passed."))
