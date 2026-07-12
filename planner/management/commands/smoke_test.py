from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.urls import reverse

from planner.feature_flags import feature_enabled
from planner.households import active_household
from planner.models import FeatureFlag
from planner.projections import build_projection


class Command(BaseCommand):
    help = "Run a small local smoke test against core pages and projection generation."

    def handle(self, *args, **options):
        household = active_household(create=False)
        if household is None:
            raise CommandError("No household exists. Run seed_demo or create a household first.")

        client = Client(HTTP_HOST="127.0.0.1")
        checks = [
            ("health", "/health/"),
            ("dashboard", reverse("planner:dashboard")),
            ("admin-login", "/admin/login/"),
        ]
        if feature_enabled("analytics"):
            checks.append(("analytics", reverse("planner:analytics")))

        for name, path in checks:
            response = client.get(path)
            if response.status_code >= 400:
                raise CommandError(f"{name} returned HTTP {response.status_code}.")
            self.stdout.write(self.style.SUCCESS(f"{name}: HTTP {response.status_code}"))

        projection = build_projection(household)
        if not projection:
            raise CommandError("Projection returned no rows.")
        self.stdout.write(self.style.SUCCESS(f"projection: {len(projection)} rows"))

        flag_count = FeatureFlag.objects.count()
        if flag_count == 0:
            raise CommandError("No feature flags exist.")
        self.stdout.write(self.style.SUCCESS(f"feature-flags: {flag_count} rows"))

        self.stdout.write(self.style.SUCCESS("Smoke test passed."))
