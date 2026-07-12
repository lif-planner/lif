from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from .models import Household


def current_month():
    today = timezone.localdate()
    return today.replace(day=1)


def active_household(create=True):
    try:
        household = Household.objects.filter(is_active=True).order_by("pk").first()
        if household:
            return household

        household = Household.objects.order_by("pk").first()
        if household:
            household.is_active = True
            household.save(update_fields=["is_active"])
            return household

        if create:
            return Household.objects.create(
                name="Home",
                start_month=current_month(),
                planning_months=24,
                planning_years=3,
                is_active=True,
            )
    except (OperationalError, ProgrammingError):
        return None
    return None
