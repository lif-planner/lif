from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand

from planner.models import Household


class Command(BaseCommand):
    help = "Create demo data only when no demo household exists."

    def add_arguments(self, parser):
        parser.add_argument("--marker-file", default="", help="Optional marker file to touch after demo data exists.")

    def handle(self, *args, **options):
        marker_file = options.get("marker_file", "")

        if Household.objects.filter(data_mode=Household.DataMode.DEMO).exists():
            self._touch_marker(marker_file)
            self.stdout.write("Demo data already present.")
            return

        call_command("seed_demo")
        self._touch_marker(marker_file)

    @staticmethod
    def _touch_marker(marker_file):
        if not marker_file:
            return
        path = Path(marker_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
