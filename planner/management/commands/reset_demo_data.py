from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

from planner.models import Household


class Command(BaseCommand):
    help = "Reset local demo households and recreate the canonical demo data."

    @transaction.atomic
    def handle(self, *args, **options):
        demo_households = Household.objects.filter(data_mode=Household.DataMode.DEMO)
        deleted_household_count = demo_households.count()
        demo_households.delete()
        call_command("seed_demo")
        self.stdout.write(
            self.style.SUCCESS(f"Reset demo data. Removed {deleted_household_count} demo household(s).")
        )
