from datetime import date
from decimal import Decimal

from planner.models import CashGoal, Household, MoneyRule, Person, RetirementPlan, SalaryChange


household, _ = Household.objects.update_or_create(
    name="My Real Household",
    defaults={
        "starting_balance": Decimal("0.00"),
        "start_month": date(2026, 7, 1),
        "planning_years": 40,
        "currency": "EUR",
    },
)

parent_1, _ = Person.objects.update_or_create(
    household=household,
    name="Parent 1",
    defaults={
        "role": Person.Role.ADULT,
        "birth_date": date(1986, 1, 1),
    },
)

parent_2, _ = Person.objects.update_or_create(
    household=household,
    name="Parent 2",
    defaults={
        "role": Person.Role.ADULT,
        "birth_date": date(1986, 1, 1),
    },
)

child_1, _ = Person.objects.update_or_create(
    household=household,
    name="Child 1",
    defaults={
        "role": Person.Role.CHILD,
        "birth_date": date(2016, 1, 1),
    },
)

child_2, _ = Person.objects.update_or_create(
    household=household,
    name="Child 2",
    defaults={
        "role": Person.Role.CHILD,
        "birth_date": date(2018, 1, 1),
    },
)

MoneyRule.objects.update_or_create(
    household=household,
    name="Salary Parent 1",
    defaults={
        "person": parent_1,
        "kind": MoneyRule.Kind.INCOME,
        "amount": Decimal("4500.00"),
        "cadence": MoneyRule.Cadence.MONTHLY,
        "start_month": date(2026, 7, 1),
        "category": "Salary",
        "is_active": True,
    },
)

MoneyRule.objects.update_or_create(
    household=household,
    name="Salary Parent 2",
    defaults={
        "person": parent_2,
        "kind": MoneyRule.Kind.INCOME,
        "amount": Decimal("2500.00"),
        "cadence": MoneyRule.Cadence.MONTHLY,
        "start_month": date(2026, 7, 1),
        "category": "Salary",
        "is_active": True,
    },
)

MoneyRule.objects.update_or_create(
    household=household,
    name="Kindergeld",
    defaults={
        "kind": MoneyRule.Kind.INCOME,
        "amount": Decimal("510.00"),
        "cadence": MoneyRule.Cadence.MONTHLY,
        "start_month": date(2026, 7, 1),
        "category": "Family",
        "is_active": True,
    },
)

MoneyRule.objects.update_or_create(
    household=household,
    name="Rent or Mortgage Cash Payment",
    defaults={
        "kind": MoneyRule.Kind.EXPENSE,
        "amount": Decimal("1800.00"),
        "cadence": MoneyRule.Cadence.MONTHLY,
        "start_month": date(2026, 7, 1),
        "category": "Housing",
        "is_active": True,
    },
)

CashGoal.objects.update_or_create(
    household=household,
    name="Base FIRE cash need",
    defaults={
        "annual_amount": Decimal("30000.00"),
        "start_year": 2026,
        "is_active": True,
    },
)

RetirementPlan.objects.update_or_create(
    household=household,
    person=parent_1,
    name="Statutory pension Parent 1",
    defaults={
        "current_pension_points": Decimal("20.000"),
        "expected_annual_points": Decimal("1.000"),
        "pension_value_per_point": Decimal("40.79"),
        "private_monthly_pension": Decimal("0.00"),
        "retirement_start_month": date(2053, 1, 1),
        "annual_adjustment_rate": Decimal("1.50"),
        "is_active": True,
    },
)

SalaryChange.objects.update_or_create(
    person=parent_1,
    name="Example future salary change",
    defaults={
        "start_month": date(2030, 1, 1),
        "monthly_net_income_delta": Decimal("500.00"),
        "is_active": True,
    },
)

print("Private seed completed. Review /quality/ before relying on projections.")
