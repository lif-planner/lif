import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.http import Http404, HttpResponse
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.test import TestCase
from django.test import RequestFactory
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .assumptions import ASSUMPTION_REVIEW_EXPIRY_DAYS
from .feature_flags import FEATURE_FLAG_DEFINITIONS, feature_enabled, feature_flag_map, feature_required
from .management.commands.seed_demo import FIRE_HOUSEHOLD_NAME, HOUSEHOLD_NAME
from lif.version import app_version
from .liquidity import build_liquidity_view, build_yearly_liquidity_view
from .models import (
    AssetAccount,
    AssumptionReview,
    BackupEvent,
    CashGoal,
    ChangeLogEntry,
    ChildMilestone,
    Debt,
    DepotHolding,
    EquityGrant,
    FamilyGiftPlan,
    FeatureFlag,
    Household,
    ImportBatch,
    IncomeInvestment,
    MoneyRule,
    MoneyMoneyAccountMapping,
    Person,
    PlannedInvestmentPurchase,
    PrivateLoanReceivable,
    RealEstate,
    RealEstateTransferPlan,
    RetirementPlan,
    SalaryChange,
    Scenario,
    Snapshot,
    SnapshotReview,
    SnapshotReviewAction,
    TransferRule,
    TrueExpense,
)
from .imports import (
    account_rows_dry_run,
    account_csv_dry_run,
    account_values_unchanged,
    apply_account_import_batch,
    apply_depot_holding_import_batch,
    depot_holding_csv_dry_run,
    dry_run_summary,
)
from .import_adapters.moneymoney import MoneyMoneyConnector, decimal_string
from .import_adapters import ImportedAccountRow, ImportedDepotHoldingRow
from .projection_integrity import check_projection_integrity
from .projections import build_projection, build_yearly_projection, summarize_debt
from .quality import build_quality_report, build_retirement_health_issues
from .retirement import retirement_tax_summary
from .sequence_risk import build_sequence_risk_summary
from .snapshots import (
    build_projection_change_drivers,
    build_snapshot_summary,
    compare_projection_summaries,
    compare_snapshot_summaries,
    compare_snapshot_to_current,
    planned_row_for_current_month,
)
from .analytics import build_analytics_data, build_analytics_milestones, build_assumption_sensitivity, build_income_timeline, build_scenario_comparison
from .finance import money_value, real_value
from .goal_planner import solve_monthly_contribution
from .households import active_household
from .mcp_data import call_tool, tool_definitions
from .moneymoney_service import (
    build_moneymoney_mapping_review,
    moneymoney_account_type_overrides,
    sync_moneymoney_mapping_rows,
)
from .readiness import (
    build_household_readiness,
    build_import_reconciliation,
    build_import_runbook,
)


class ProjectionTests(TestCase):
    def test_owned_real_estate_counts_as_illiquid_net_worth_and_carries_costs(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
            fund_cash_goal_from_depot=True,
        )
        cash = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("10000.00"),
        )
        mortgage_account = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("250000.00"),
        )
        mortgage = Debt.objects.create(
            household=household,
            account=mortgage_account,
            name="Mortgage",
            current_principal=Decimal("250000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("0.00"),
            start_month=date(2026, 1, 1),
        )
        home = RealEstate.objects.create(
            household=household,
            name="Home",
            current_value=Decimal("400000.00"),
            annual_appreciation_rate=Decimal("12.00"),
            source_account=cash,
            monthly_costs=Decimal("300.00"),
        )
        mortgage.real_estate = home
        mortgage.save()

        projection = build_projection(household)

        self.assertEqual(projection[0].opening_other_asset_balance, Decimal("400000.00"))
        self.assertEqual(projection[0].opening_liability_balance, Decimal("250000.00"))
        self.assertEqual(projection[0].real_estate_costs, Decimal("300.00"))
        self.assertEqual(projection[0].expenses, Decimal("300.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("9700.00"))
        self.assertEqual(projection[0].other_asset_balance, Decimal("403795.52"))
        self.assertEqual(projection[0].real_estate_appreciation, Decimal("3795.52"))

    def test_residence_saved_rent_offsets_modeled_housing_expense(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        cash = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        household.default_operating_account = cash
        household.save(update_fields=["default_operating_account"])
        RealEstate.objects.create(
            household=household,
            name="Home",
            use=RealEstate.Use.RESIDENCE,
            current_value=Decimal("300000.00"),
            monthly_costs=Decimal("200.00"),
            saved_monthly_rent=Decimal("900.00"),
            source_account=cash,
        )
        MoneyRule.objects.create(
            household=household,
            name="Comparable rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("900.00"),
        )

        projection = build_projection(household)

        sections = {line.section: line for line in projection[0].audit_lines}
        self.assertEqual(projection[0].expenses, Decimal("200.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("800.00"))
        self.assertEqual(sections["Saved rent"].cash_effect, Decimal("900.00"))

    def test_property_transfer_removes_value_but_retained_niessbrauch_keeps_saved_rent(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        parent = Person.objects.create(household=household, name="Parent", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Child", role=Person.Role.CHILD)
        cash = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        household.default_operating_account = cash
        household.save(update_fields=["default_operating_account"])
        home = RealEstate.objects.create(
            household=household,
            name="Flat",
            use=RealEstate.Use.RESIDENCE,
            current_value=Decimal("300000.00"),
            annual_appreciation_rate=Decimal("0.00"),
            monthly_costs=Decimal("200.00"),
            saved_monthly_rent=Decimal("900.00"),
            source_account=cash,
        )
        RealEstateTransferPlan.objects.create(
            household=household,
            property_item=home,
            giver=parent,
            recipient=child,
            name="Gift flat to child",
            transfer_month=date(2026, 2, 1),
            ownership_percent=Decimal("100.00"),
            taxable_gift_value=Decimal("180000.00"),
            retained_niessbrauch=True,
            niessbrauch_annual_value=Decimal("10800.00"),
        )

        projection = build_projection(household)
        january = projection[0]
        february = projection[1]
        march = projection[2]

        self.assertEqual(january.net_worth, Decimal("301700.00"))
        self.assertEqual(february.other_asset_balance, Decimal("0.00"))
        self.assertEqual(february.net_worth, Decimal("2400.00"))
        self.assertEqual(march.other_asset_balance, Decimal("0.00"))
        self.assertEqual(march.real_estate_costs, Decimal("-700.00"))
        self.assertEqual(march.net_worth, Decimal("3100.00"))
        transfer_line = next(line for line in february.audit_lines if line.section == "Property transfer")
        self.assertEqual(transfer_line.name, "Gift flat to child")
        self.assertEqual(transfer_line.other_asset_effect, Decimal("-300000.00"))
        self.assertIn("Nießbrauch retained", transfer_line.note)

    def test_property_transfer_pages_and_snapshot_show_plan(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        parent = Person.objects.create(household=household, name="Parent", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Child", role=Person.Role.CHILD)
        home = RealEstate.objects.create(
            household=household,
            name="Flat",
            use=RealEstate.Use.RESIDENCE,
            current_value=Decimal("300000.00"),
        )
        RealEstateTransferPlan.objects.create(
            household=household,
            property_item=home,
            giver=parent,
            recipient=child,
            name="Gift flat to child",
            transfer_month=date(2028, 1, 1),
            ownership_percent=Decimal("100.00"),
            taxable_gift_value=Decimal("180000.00"),
            retained_niessbrauch=True,
        )

        property_response = self.client.get(reverse("planner:real_estate_index"))
        plan_response = self.client.get(reverse("planner:plan_index"))
        summary = build_snapshot_summary(household)

        self.assertContains(property_response, "Property Transfers")
        self.assertContains(property_response, "Gift flat to child")
        self.assertContains(property_response, "180,000.00 EUR")
        self.assertContains(plan_response, "Property transfer")
        self.assertContains(plan_response, "Nießbrauch retained")
        self.assertEqual(summary["counts"]["real_estate_transfer_plans"], 1)
        self.assertEqual(summary["real_estate_transfer_plans"][0]["name"], "Gift flat to child")
        self.assertTrue(summary["real_estate_transfer_plans"][0]["retained_niessbrauch"])

    def test_future_property_purchase_appears_at_acquisition_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Savings",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("80000.00"),
        )
        mortgage_account = AssetAccount.objects.create(
            household=household,
            name="Future mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("250000.00"),
        )
        mortgage = Debt.objects.create(
            household=household,
            account=mortgage_account,
            name="Future mortgage",
            current_principal=Decimal("250000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("0.00"),
            start_month=date(2026, 3, 1),
        )
        future_home = RealEstate.objects.create(
            household=household,
            name="Future home",
            current_value=Decimal("300000.00"),
            acquisition_month=date(2026, 2, 1),
            down_payment=Decimal("50000.00"),
            acquisition_costs=Decimal("10000.00"),
            source_account=savings,
        )
        mortgage.real_estate = future_home
        mortgage.save()

        projection = build_projection(household)

        self.assertEqual(projection[0].other_asset_balance, Decimal("0.00"))
        self.assertEqual(projection[0].liability_balance, Decimal("0.00"))
        self.assertEqual(projection[1].opening_other_asset_balance, Decimal("0.00"))
        self.assertEqual(projection[1].other_asset_balance, Decimal("300000.00"))
        self.assertEqual(projection[1].liability_balance, Decimal("250000.00"))
        self.assertEqual(projection[1].liquid_balance, Decimal("20000.00"))
        self.assertEqual(projection[1].net_worth, Decimal("70000.00"))

    def test_property_sale_releases_value_pays_mortgage_and_lands_cash(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        cash = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("10000.00"),
        )
        mortgage_account = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        mortgage = Debt.objects.create(
            household=household,
            account=mortgage_account,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("0.00"),
            start_month=date(2026, 1, 1),
        )
        flat = RealEstate.objects.create(
            household=household,
            name="Flat",
            current_value=Decimal("300000.00"),
            sale_month=date(2026, 1, 1),
            sale_costs_rate=Decimal("2.00"),
            capital_gains_tax_rate=Decimal("0.00"),
            sale_proceeds_account=cash,
        )
        mortgage.real_estate = flat
        mortgage.save()

        projection = build_projection(household)

        self.assertEqual(projection[0].real_estate_sale_proceeds, Decimal("200000.00"))
        self.assertEqual(projection[0].real_estate_costs, Decimal("6000.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("204000.00"))
        self.assertEqual(projection[0].other_asset_balance, Decimal("0.00"))
        self.assertEqual(projection[0].liability_balance, Decimal("0.00"))
        self.assertEqual(projection[0].net, Decimal("194000.00"))

    def test_property_can_carry_two_mortgages_both_paid_off_on_sale(self):
        # A property can have several loans (e.g. a main mortgage + a KfW loan);
        # both count as liabilities and both are paid off when it is sold.
        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=1
        )
        cash = AssetAccount.objects.create(
            household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("0.00")
        )
        loan1 = AssetAccount.objects.create(
            household=household, name="Main loan", account_type=AssetAccount.AccountType.LOAN, balance=Decimal("200000.00")
        )
        loan2 = AssetAccount.objects.create(
            household=household, name="KfW loan", account_type=AssetAccount.AccountType.LOAN, balance=Decimal("50000.00")
        )
        debt1 = Debt.objects.create(
            household=household, account=loan1, name="Main mortgage", current_principal=Decimal("200000.00"),
            annual_interest_rate=Decimal("0.00"), monthly_payment=Decimal("0.00"), start_month=date(2026, 1, 1),
        )
        debt2 = Debt.objects.create(
            household=household, account=loan2, name="KfW", current_principal=Decimal("50000.00"),
            annual_interest_rate=Decimal("0.00"), monthly_payment=Decimal("0.00"), start_month=date(2026, 1, 1),
        )
        flat = RealEstate.objects.create(
            household=household, name="Apartment", current_value=Decimal("400000.00"),
            sale_month=date(2026, 1, 1), sale_costs_rate=Decimal("0.00"),
            capital_gains_tax_rate=Decimal("0.00"), sale_proceeds_account=cash,
        )
        debt1.real_estate = flat
        debt1.save()
        debt2.real_estate = flat
        debt2.save()

        jan = build_projection(household)[0]

        # Both loans (250k) are paid off on sale; net proceeds = 400k - 250k = 150k.
        self.assertEqual(jan.liability_balance, Decimal("0.00"))
        self.assertEqual(jan.real_estate_sale_proceeds, Decimal("150000.00"))
        self.assertEqual(jan.liquid_balance, Decimal("150000.00"))

    def test_future_residence_lifecycle_reconciles_purchase_debt_costs_saved_rent_and_sale(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=5,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("100000.00"),
        )
        mortgage_account = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("250000.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        mortgage = Debt.objects.create(
            household=household,
            account=mortgage_account,
            source_account=giro,
            name="Mortgage",
            current_principal=Decimal("250000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("1000.00"),
            start_month=date(2026, 2, 1),
        )
        home = RealEstate.objects.create(
            household=household,
            name="Future home",
            use=RealEstate.Use.RESIDENCE,
            current_value=Decimal("300000.00"),
            acquisition_month=date(2026, 2, 1),
            down_payment=Decimal("50000.00"),
            acquisition_costs=Decimal("10000.00"),
            source_account=giro,
            monthly_costs=Decimal("300.00"),
            saved_monthly_rent=Decimal("1000.00"),
            sale_month=date(2026, 5, 1),
            sale_costs_rate=Decimal("0.00"),
            capital_gains_tax_rate=Decimal("0.00"),
            sale_proceeds_account=giro,
        )
        mortgage.real_estate = home
        mortgage.save()

        projection = build_projection(household)
        yearly = build_yearly_projection(projection)
        result = check_projection_integrity(projection, yearly, household.accounts.all())

        self.assertEqual(projection[0].liquid_balance, Decimal("100000.00"))
        self.assertEqual(projection[0].other_asset_balance, Decimal("0.00"))
        self.assertEqual(projection[0].liability_balance, Decimal("0.00"))

        february = projection[1]
        february_sections = {line.section for line in february.audit_lines}
        self.assertIn("Property purchase", february_sections)
        self.assertIn("Property costs", february_sections)
        self.assertIn("Saved rent", february_sections)
        self.assertEqual(february.liquid_balance, Decimal("39700.00"))
        self.assertEqual(february.other_asset_balance, Decimal("300000.00"))
        self.assertEqual(february.liability_balance, Decimal("249000.00"))
        self.assertEqual(february.real_estate_costs, Decimal("9300.00"))
        self.assertEqual(february.debt_principal, Decimal("1000.00"))
        self.assertEqual(february.account_balances[giro.id], Decimal("39700.00"))
        self.assertEqual(february.account_balances[mortgage_account.id], Decimal("249000.00"))

        self.assertEqual(projection[2].liquid_balance, Decimal("39400.00"))
        self.assertEqual(projection[2].liability_balance, Decimal("248000.00"))
        self.assertEqual(projection[3].liquid_balance, Decimal("39100.00"))
        self.assertEqual(projection[3].liability_balance, Decimal("247000.00"))

        may = projection[4]
        sale_line = next(line for line in may.audit_lines if line.section == "Property sale")
        self.assertEqual(may.real_estate_sale_proceeds, Decimal("53000.00"))
        self.assertEqual(may.liquid_balance, Decimal("92100.00"))
        self.assertEqual(may.other_asset_balance, Decimal("0.00"))
        self.assertEqual(may.liability_balance, Decimal("0.00"))
        self.assertEqual(may.debt_principal, Decimal("0.00"))
        self.assertEqual(may.account_balances[giro.id], Decimal("92100.00"))
        self.assertEqual(may.account_balances[mortgage_account.id], Decimal("0.00"))
        self.assertEqual(sale_line.note, "0.00 sale costs, 247000.00 mortgage payoff, 0.00 capital gains tax, to Giro")
        self.assertTrue(result["ok"], result["failures"])

    def test_investment_property_adds_net_rental_income(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        cash = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        RealEstate.objects.create(
            household=household,
            name="Rental flat",
            use=RealEstate.Use.INVESTMENT,
            current_value=Decimal("200000.00"),
            monthly_rent=Decimal("1000.00"),
            vacancy_rate=Decimal("10.00"),
            rent_tax_rate=Decimal("25.00"),
            source_account=cash,
        )

        month = build_projection(household)[0]

        self.assertEqual(month.rental_income, Decimal("675.00"))
        self.assertEqual(month.income, Decimal("675.00"))
        self.assertEqual(month.liquid_balance, Decimal("1675.00"))

    def test_planning_years_extend_projection_and_auto_yearly_display(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=6,
            planning_years=2,
        )

        projection = build_projection(household)

        self.assertEqual(household.projection_months, 24)
        self.assertEqual(len(projection), 24)
        self.assertEqual(projection[-1].month, date(2027, 12, 1))
        self.assertEqual(household.resolved_display_granularity, Household.DisplayGranularity.MONTHLY)

    def test_yearly_projection_aggregates_monthly_projection(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=14,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("700.00"),
        )

        yearly_projection = build_yearly_projection(build_projection(household))
        liquidity_view = build_yearly_liquidity_view(yearly_projection)

        self.assertEqual(len(yearly_projection), 2)
        self.assertEqual(yearly_projection[0].year, 2026)
        self.assertEqual(yearly_projection[0].income, Decimal("12000.00"))
        self.assertEqual(yearly_projection[0].expenses, Decimal("8400.00"))
        self.assertEqual(yearly_projection[0].ending_liquid_balance, Decimal("4600.00"))
        self.assertEqual(yearly_projection[1].year, 2027)
        self.assertEqual(yearly_projection[1].income, Decimal("2000.00"))
        self.assertEqual(liquidity_view["months"][0]["label"], "2026")

    def test_yearly_projection_includes_audit_rollup(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1200.00"),
        )

        year = build_yearly_projection(build_projection(household))[0]
        lines = {line.name: line for line in year.audit_lines}

        self.assertEqual(year.start_index, 0)
        self.assertEqual(year.end_index, 11)
        self.assertEqual(year.month_count, 12)
        self.assertEqual(year.opening_liquid_balance, Decimal("1000.00"))
        self.assertEqual(year.ending_liquid_balance, Decimal("22600.00"))
        self.assertEqual(lines["Salary"].cash_effect, Decimal("36000.00"))
        self.assertEqual(lines["Rent"].cash_effect, Decimal("-14400.00"))

    def test_yearly_projection_compares_income_to_cash_goal(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Pension",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("2000.00"),
        )
        CashGoal.objects.create(
            household=household,
            name="FIRE need",
            annual_amount=Decimal("30000.00"),
            start_year=2026,
        )

        year = build_yearly_projection(build_projection(household), household.cash_goals.all())[0]

        self.assertEqual(year.income, Decimal("24000.00"))
        self.assertEqual(year.annual_cash_goal, Decimal("30000.00"))
        self.assertEqual(year.cash_goal_gap, Decimal("6000.00"))
        self.assertEqual(year.cash_goal_coverage_percent, Decimal("80.00"))
        self.assertEqual(year.portfolio_draw_percent, Decimal("12.00"))

    def test_yearly_projection_indexes_cash_goals_to_inflation(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_years=3,
            annual_inflation_rate=Decimal("10.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Income",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"),
        )
        CashGoal.objects.create(
            household=household,
            name="Indexed need",
            annual_amount=Decimal("12000.00"),
            indexed_to_inflation=True,
            start_year=2026,
        )

        yearly_projection = build_yearly_projection(build_projection(household), household.cash_goals.all())

        self.assertEqual(yearly_projection[0].annual_cash_goal, Decimal("12000.00"))
        self.assertEqual(yearly_projection[1].annual_cash_goal, Decimal("13200.00"))
        self.assertEqual(yearly_projection[2].annual_cash_goal, Decimal("14520.00"))

    def test_non_january_bucket_splits_cash_goal_across_calendar_years(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 7, 1),
            planning_years=1,
        )
        CashGoal.objects.create(
            household=household,
            name="Starts next year",
            annual_amount=Decimal("12000.00"),
            start_year=2027,
        )

        years = build_yearly_projection(build_projection(household), household.cash_goals.all())

        # Calendar-year buckets: a July start splits into a partial 2026 (Jul-Dec)
        # and a partial 2027 (Jan-Jun). The goal starts 2027, so it lands entirely
        # in the 2027 bucket: 6 x (12000 / 12) = 6000.
        self.assertEqual([y.label for y in years], ["2026 (6 mo)", "2027 (6 mo)"])
        self.assertEqual(years[0].annual_cash_goal, Decimal("0.00"))
        self.assertEqual(years[1].annual_cash_goal, Decimal("6000.00"))

    def test_overlapping_cash_goals_use_most_recent_start(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_years=2,
        )
        CashGoal.objects.create(household=household, name="Original", annual_amount=Decimal("30000.00"), start_year=2026)
        CashGoal.objects.create(household=household, name="Updated", annual_amount=Decimal("40000.00"), start_year=2027)

        yearly = build_yearly_projection(build_projection(household), household.cash_goals.all())

        # 2026: only the original goal applies. 2027: both overlap, the later
        # start year (Updated) wins -- not the earlier one as before.
        self.assertEqual(yearly[0].annual_cash_goal, Decimal("30000.00"))
        self.assertEqual(yearly[1].annual_cash_goal, Decimal("40000.00"))

    def test_quality_warns_on_overlapping_cash_goals(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        CashGoal.objects.create(household=household, name="A", annual_amount=Decimal("30000.00"), start_year=2026)
        CashGoal.objects.create(household=household, name="B", annual_amount=Decimal("40000.00"), start_year=2027)

        titles = {item.title for item in build_quality_report(household)["issues"]}
        self.assertIn("Cash goals overlap", titles)

        # Non-overlapping goals (A ends before B starts) do not warn.
        CashGoal.objects.filter(household=household).delete()
        CashGoal.objects.create(
            household=household, name="Early", annual_amount=Decimal("30000.00"), start_year=2026, end_year=2026
        )
        CashGoal.objects.create(household=household, name="Late", annual_amount=Decimal("40000.00"), start_year=2027)
        titles2 = {item.title for item in build_quality_report(household)["issues"]}
        self.assertNotIn("Cash goals overlap", titles2)

    def test_account_form_rejects_non_household_currency(self):
        from planner.forms import AssetAccountForm

        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        base = {"name": "Broker", "account_type": AssetAccount.AccountType.CASH, "balance": "100.00"}

        bad = AssetAccountForm(data={**base, "currency": "USD"}, household=household)
        self.assertFalse(bad.is_valid())
        self.assertIn("currency", bad.errors)

        # The household currency passes the currency check (other unrelated
        # required fields are not part of this assertion).
        good = AssetAccountForm(data={**base, "currency": "eur"}, household=household)
        good.is_valid()
        self.assertNotIn("currency", good.errors)

    def test_quality_warns_on_non_household_currency(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        AssetAccount.objects.create(
            household=household,
            name="US Cash",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("100.00"),
            currency="USD",
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            currency="EUR",
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="US ETF",
            quantity=Decimal("1.000000"),
            latest_price=Decimal("100.00"),
            currency="USD",
        )

        titles = {item.title for item in build_quality_report(household)["issues"]}
        self.assertIn("US Cash uses a non-household currency", titles)
        self.assertIn("US ETF uses a non-household currency", titles)

    def test_yearly_projection_uses_calendar_year_buckets_for_long_horizons(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_years=40,
        )

        yearly_projection = build_yearly_projection(build_projection(household))

        # June 2026 start over 40 years (480 months -> through May 2066): a partial
        # 2026 (Jun-Dec, 7 mo), 39 full calendar years, and a partial 2066 (Jan-May).
        self.assertEqual(len(yearly_projection), 41)
        self.assertEqual(yearly_projection[0].label, "2026 (7 mo)")
        self.assertEqual(yearly_projection[1].label, "2027")
        self.assertEqual(yearly_projection[-2].label, "2065")
        self.assertEqual(yearly_projection[-1].label, "2066 (5 mo)")

    def test_monthly_income_and_expense_update_balance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 6, 1),
            planning_months=2,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1200.00"),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].net, Decimal("1800.00"))
        self.assertEqual(projection[0].balance, Decimal("2800.00"))
        self.assertEqual(projection[1].balance, Decimal("4600.00"))

    def test_projection_month_includes_audit_lines_and_opening_balance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        household.default_operating_account = account
        household.save(update_fields=["default_operating_account"])
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1200.00"),
        )

        month = build_projection(household)[0]

        self.assertEqual(month.opening_liquid_balance, Decimal("1000.00"))
        self.assertEqual(month.liquid_balance, Decimal("2800.00"))
        self.assertEqual(len(month.audit_lines), 2)
        lines = {line.name: line for line in month.audit_lines}
        self.assertEqual(lines["Salary"].cash_effect, Decimal("3000.00"))
        self.assertEqual(lines["Rent"].cash_effect, Decimal("-1200.00"))
        self.assertEqual(lines["Salary"].account_effects[0]["account_name"], "Giro")
        self.assertEqual(lines["Salary"].account_effects[0]["amount"], Decimal("3000.00"))
        self.assertEqual(lines["Rent"].account_effects[0]["amount"], Decimal("-1200.00"))

    def test_projection_integrity_checks_reconcile_clean_projection(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_annual_return_rate=Decimal("3.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
            start_month=date(2026, 1, 1),
        )
        TransferRule.objects.create(
            household=household,
            name="ETF top-up",
            amount=Decimal("500.00"),
            source_account=giro,
            target_account=depot,
            start_month=date(2026, 1, 1),
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("24000.00"), start_year=2026)

        projection = build_projection(household)
        yearly = build_yearly_projection(projection, household.cash_goals.all())
        result = check_projection_integrity(projection, yearly, household.accounts.all())

        self.assertTrue(result["ok"])
        self.assertGreater(result["checked"], 0)
        self.assertEqual(result["failure_count"], 0)

    def test_projection_integrity_includes_real_estate_sale_proceeds_in_net_cash(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("10000.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        RealEstate.objects.create(
            household=household,
            name="Flat",
            use=RealEstate.Use.INVESTMENT,
            current_value=Decimal("200000.00"),
            sale_month=date(2026, 1, 1),
            sale_proceeds_account=giro,
        )

        projection = build_projection(household)
        yearly = build_yearly_projection(projection)
        result = check_projection_integrity(projection, yearly, household.accounts.all())

        self.assertEqual(projection[0].real_estate_sale_proceeds, Decimal("200000.00"))
        self.assertEqual(projection[0].net, Decimal("200000.00"))
        self.assertTrue(result["ok"], result["failures"])

    def test_projection_integrity_reconciles_mixed_account_flows(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=4,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("10000.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("5000.00"),
            savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.QUARTERLY,
            savings_interest_tax_rate=Decimal("25.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
            depot_annual_return_rate=Decimal("0.00"),
            depot_teilfreistellung_rate=Decimal("0.00"),
        )
        loan_account = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("3000.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        Debt.objects.create(
            household=household,
            account=loan_account,
            source_account=giro,
            name="Mortgage",
            current_principal=Decimal("3000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
        )
        MoneyRule.objects.create(
            household=household,
            account=giro,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("2000.00"),
            start_month=date(2026, 1, 1),
        )
        MoneyRule.objects.create(
            household=household,
            account=giro,
            name="Groceries",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("300.00"),
            start_month=date(2026, 1, 1),
        )
        TransferRule.objects.create(
            household=household,
            name="Cash buffer",
            amount=Decimal("500.00"),
            cadence=TransferRule.Cadence.ONCE,
            source_account=giro,
            target_account=savings,
            start_month=date(2026, 1, 1),
        )
        TransferRule.objects.create(
            household=household,
            name="ETF savings plan",
            amount=Decimal("100.00"),
            source_account=savings,
            target_account=depot,
            start_month=date(2026, 1, 1),
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            source_account=savings,
            name="Family loan",
            borrower="Sibling",
            current_principal=Decimal("1200.00"),
            monthly_principal_repayment=Decimal("400.00"),
            disbursement_month=date(2026, 2, 1),
            start_month=date(2026, 3, 1),
            end_month=date(2026, 4, 1),
            is_active=True,
        )
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="Target bond",
            asset_type=PlannedInvestmentPurchase.AssetType.BOND,
            source_account=savings,
            target_account=depot,
            purchase_amount=Decimal("1000.00"),
            purchase_month=date(2026, 3, 1),
            payout_date=date(2026, 4, 1),
            payout_amount=Decimal("1100.00"),
        )

        projection = build_projection(household)
        yearly = build_yearly_projection(projection)
        result = check_projection_integrity(projection, yearly, household.accounts.all())

        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(projection[0].account_balances[giro.id], Decimal("10200.00"))
        self.assertEqual(projection[0].account_balances[savings.id], Decimal("5400.00"))
        self.assertEqual(projection[0].account_balances[depot.id], Decimal("1100.00"))
        self.assertEqual(projection[0].account_balances[loan_account.id], Decimal("2000.00"))
        self.assertEqual(projection[2].savings_interest_income, Decimal("101.25"))
        self.assertEqual(projection[2].account_balances[savings.id], Decimal("3501.25"))
        self.assertEqual(projection[2].account_balances[depot.id], Decimal("2300.00"))
        self.assertEqual(projection[3].depot_payout, Decimal("1075.00"))
        self.assertEqual(projection[3].account_balances[giro.id], Decimal("14375.00"))
        self.assertEqual(projection[3].account_balances[savings.id], Decimal("4201.25"))
        self.assertEqual(projection[3].account_balances[depot.id], Decimal("1400.00"))
        self.assertEqual(projection[3].account_balances[loan_account.id], Decimal("0.00"))
        self.assertEqual(projection[3].other_asset_balance, Decimal("0.00"))

    def test_projection_integrity_allows_general_liquid_pool(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Unrouted salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("250.00"),
        )

        projection = build_projection(household)
        result = check_projection_integrity(projection, accounts=household.accounts.all())

        self.assertTrue(result["ok"])
        self.assertEqual(projection[0].liquid_balance, Decimal("1250.00"))
        self.assertEqual(sum(projection[0].account_balances.values()), Decimal("1000.00"))

    def test_projection_integrity_detects_broken_liquid_account_snapshot(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("250.00"),
        )
        projection = build_projection(household)
        broken_projection = [
            replace(
                projection[0],
                account_balances={**projection[0].account_balances, giro.id: Decimal("999.00")},
            )
        ]

        result = check_projection_integrity(broken_projection, accounts=household.accounts.all())

        self.assertFalse(result["ok"])
        self.assertIn(
            "Liquid accounts plus general pool sum to liquid bucket",
            {failure["check"] for failure in result["failures"]},
        )

    def test_projection_integrity_checks_detect_broken_projection(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
            start_month=date(2026, 1, 1),
        )
        projection = build_projection(household)
        broken_projection = [
            replace(
                projection[0],
                liquid_balance=projection[0].liquid_balance + Decimal("100.00"),
            )
        ]

        result = check_projection_integrity(broken_projection, accounts=household.accounts.all())

        self.assertFalse(result["ok"])
        self.assertIn("Liquid bridge matches audit cash effects", {failure["check"] for failure in result["failures"]})

    def test_projection_audit_page_renders_month_calculation(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        household = Household.objects.get()
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )

        response = self.client.get(reverse("planner:projection_audit", args=[0]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Forecast detail")
        self.assertContains(response, "Main Drivers This Period")
        self.assertContains(response, "Major cash movements")
        self.assertContains(response, "Explanation Summary")
        self.assertContains(response, "Main cash drivers")
        self.assertContains(response, "Liquid change")
        self.assertContains(response, "What Drove This Month")
        self.assertContains(response, "Work and recurring income")
        self.assertContains(response, "Opening liquid balance")
        self.assertContains(response, "Accounts")
        self.assertContains(response, "Salary")

    def test_projection_audit_can_show_today_money(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=13,
            annual_inflation_rate=Decimal("10.00"),
        )
        household = Household.objects.get()
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1200.00"),
        )

        response = self.client.get(reverse("planner:projection_audit", args=[12]), {"display": "real"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Today's money")
        self.assertContains(response, "today's money, 10.00% inflation")

    def test_projection_integrity_page_summarizes_reconciliation(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:projection_integrity"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Projection Integrity")
        self.assertContains(response, "All checks passed.")
        self.assertContains(response, "Bucket Bridge")
        self.assertContains(response, "Liquid balance")
        self.assertContains(response, "Net worth")
        self.assertContains(response, reverse("planner:data_quality"))
        self.assertContains(response, reverse("planner:analytics"))

    def test_projection_audit_page_surfaces_cash_shortfall_warning(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        household = Household.objects.get()
        MoneyRule.objects.create(
            household=household,
            name="Large expense",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("3000.00"),
        )

        response = self.client.get(reverse("planner:projection_audit", args=[0]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Things To Check")
        self.assertContains(response, "Liquid cash goes negative")
        self.assertContains(response, "Costs and debt interest")

    def test_projection_year_audit_page_renders_rollup_and_months(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        household = Household.objects.get()
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )

        response = self.client.get(reverse("planner:projection_year_audit", args=[0]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Year forecast detail")
        self.assertContains(response, "Main Drivers This Period")
        self.assertContains(response, "Major cash movements")
        self.assertContains(response, "Explanation Summary")
        self.assertContains(response, "Cash bridge")
        self.assertContains(response, "What Drove This Year")
        self.assertContains(response, "Work and recurring income")
        self.assertContains(response, "Raw Calculation Rollup")
        self.assertContains(response, "Monthly Breakdown")
        self.assertContains(response, "Salary")

    def test_projection_year_audit_can_show_today_money(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=2,
            annual_inflation_rate=Decimal("10.00"),
        )
        household = Household.objects.get()
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1200.00"),
        )

        response = self.client.get(reverse("planner:projection_year_audit", args=[1]), {"display": "real"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Today's money")
        self.assertContains(response, "today's money, 10.00% inflation")

    def test_assumptions_registry_page_renders_planning_knobs(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=5,
            annual_inflation_rate=Decimal("2.50"),
            default_income_growth_rate=Decimal("1.50"),
            capital_gains_tax_rate=Decimal("25.00"),
            fund_cash_goal_from_depot=True,
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_annual_return_rate=Decimal("5.00"),
            depot_annual_distribution_rate=Decimal("1.20"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"),
            savings_annual_interest_rate=Decimal("2.50"),
        )
        loan_account = AssetAccount.objects.create(
            household=household,
            name="Loan",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        Debt.objects.create(
            household=household,
            account=loan_account,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"),
            refinance_annual_interest_rate=Decimal("4.50"),
            refinance_monthly_payment=Decimal("1200.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
            annual_growth_rate=Decimal("2.00"),
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("30000.00"), start_year=2026)
        RetirementPlan.objects.create(
            household=household,
            person=person,
            name="Pension",
            current_pension_points=Decimal("10.000"),
            retirement_start_month=date(2050, 1, 1),
            annual_adjustment_rate=Decimal("1.00"),
        )

        response = self.client.get(reverse("planner:assumptions_registry"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assumptions")
        self.assertContains(response, "Household defaults")
        self.assertContains(response, "60 months")
        self.assertContains(response, "Tax and deduction assumptions")
        self.assertContains(response, "Depot assumptions")
        self.assertContains(response, "Tagesgeld")
        self.assertContains(response, "Debt and refinance assumptions")
        self.assertContains(response, "Income growth assumptions")
        self.assertContains(response, "Cash goal assumptions")
        self.assertContains(response, "Retirement assumptions")
        self.assertContains(response, "Depot draw")
        self.assertContains(response, "Reviewed")
        self.assertContains(response, "Defaults")
        self.assertContains(response, "Inherited")
        self.assertContains(response, "Expired")
        self.assertContains(response, "Review note")
        self.assertContains(response, "Review group")
        self.assertContains(response, reverse("planner:assumption_review_edit"))
        self.assertContains(response, "Custom household inflation assumption.")
        self.assertContains(response, "Still using the app default pension-tax assumption.")
        self.assertContains(response, "Income rule has its own growth rate.")
        self.assertContains(response, "Impact")
        self.assertContains(response, "Changes projected invested balance, FIRE readiness, retirement drawdown")
        self.assertContains(response, "Changes net ETF/depot withdrawals, distributions, bond interest")
        self.assertContains(response, "Edit source")

    def test_assumptions_registry_can_mark_rows_and_groups_reviewed(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=5,
        )

        form_response = self.client.get(
            reverse("planner:assumption_review_edit"),
            {"review_key": "household:inflation"},
        )

        self.assertEqual(form_response.status_code, 200)
        self.assertContains(form_response, "Review assumption")
        self.assertContains(form_response, "Inflation")
        self.assertContains(form_response, "Review note")
        self.assertContains(form_response, "Moves real purchasing-power charts")

        row_response = self.client.post(
            reverse("planner:assumption_review_edit"),
            {
                "scope": "row",
                "review_key": "household:inflation",
                "reviewed_by": "Christian",
                "note": "Checked with current planning baseline.",
            },
        )

        self.assertRedirects(row_response, reverse("planner:assumptions_registry"))
        review = AssumptionReview.objects.get(household=household, key="household:inflation")
        self.assertEqual(review.label, "Inflation")
        self.assertEqual(review.reviewed_by, "Christian")
        self.assertEqual(review.note, "Checked with current planning baseline.")

        response = self.client.get(reverse("planner:assumptions_registry"))
        self.assertContains(response, "Reviewed on")
        self.assertContains(response, "Checked with current planning baseline.")

        group_response = self.client.post(
            reverse("planner:assumption_review_edit"),
            {"scope": "group", "group_key": "tax", "note": "Reviewed tax assumptions together."},
        )

        self.assertRedirects(group_response, reverse("planner:assumptions_registry"))
        self.assertTrue(AssumptionReview.objects.filter(household=household, key="tax:pension-tax").exists())
        self.assertTrue(AssumptionReview.objects.filter(household=household, key="tax:capital-gains-tax").exists())

    def test_expired_assumption_reviews_surface_in_quality_attention_and_checklist(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=5,
        )
        review = AssumptionReview.objects.create(
            household=household,
            key="household:inflation",
            label="Inflation",
            reviewed_by="Christian",
        )
        AssumptionReview.objects.filter(pk=review.pk).update(
            reviewed_at=timezone.now() - timedelta(days=400)
        )

        assumptions_response = self.client.get(reverse("planner:assumptions_registry"))
        self.assertContains(assumptions_response, "Expired")
        self.assertContains(assumptions_response, "Last reviewed")

        quality_response = self.client.get(reverse("planner:data_quality"))
        self.assertContains(quality_response, "Expired assumption reviews")
        self.assertContains(quality_response, reverse("planner:assumption_review_center"))

        dashboard_response = self.client.get(reverse("planner:dashboard"))
        self.assertContains(dashboard_response, "Assumption reviews expired")
        self.assertContains(dashboard_response, "Review assumptions")
        self.assertContains(dashboard_response, reverse("planner:assumption_review_center"))

        center_response = self.client.get(reverse("planner:assumption_review_center"))
        self.assertContains(center_response, "Assumption review center")
        self.assertContains(center_response, "Needs review")
        self.assertContains(center_response, "Expired reviews")
        self.assertContains(center_response, "Inflation")
        self.assertContains(center_response, "Moves real purchasing-power charts")
        self.assertContains(center_response, reverse("planner:assumption_review_edit"))

        checklist_response = self.client.get(reverse("planner:planning_review_checklist"))
        self.assertContains(checklist_response, "Trust checklist")
        self.assertContains(checklist_response, "Assumption reviews")
        self.assertContains(checklist_response, "1 expired review(s).")
        self.assertContains(checklist_response, "Mark all expired reviewed")
        self.assertContains(checklist_response, "Scenario confidence")

        post_response = self.client.post(
            reverse("planner:planning_review_checklist"),
            {
                "action": "mark_expired_assumptions_reviewed",
                "reviewed_by": "Christian",
                "note": "Annual review completed.",
            },
        )
        self.assertRedirects(post_response, reverse("planner:planning_review_checklist"))
        review.refresh_from_db()
        self.assertEqual(review.reviewed_by, "Christian")
        self.assertEqual(review.note, "Annual review completed.")
        refreshed_response = self.client.get(reverse("planner:planning_review_checklist"))
        self.assertContains(refreshed_response, "0 expired review(s).")

    def test_assumptions_registry_shows_holding_distribution_summary(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=depot, name="Accumulating fund", quantity=Decimal("10.000000"), latest_price=Decimal("500.00"),
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Distributing fund",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("500.00"),
            annual_distribution_rate=Decimal("2.00"),
        )

        response = self.client.get(reverse("planner:assumptions_registry"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 of 2 holdings distribute")

    def test_analytics_data_contains_monthly_and_yearly_series(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            annual_inflation_rate=Decimal("2.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Statutory pension",
            current_pension_points=Decimal("10.000"),
            expected_annual_points=Decimal("1.000"),
            retirement_start_month=date(2036, 1, 1),
        )

        projection = build_projection(household)
        yearly_projection = build_yearly_projection(projection)
        analytics_data = build_analytics_data(projection, yearly_projection, household)

        self.assertEqual(len(analytics_data["monthly"]), 12)
        self.assertEqual(len(analytics_data["yearly"]), 1)
        self.assertEqual(analytics_data["monthly"][0]["label"], "Jan 2026")
        self.assertEqual(analytics_data["monthly"][0]["detailUrl"], reverse("planner:projection_audit", args=[0]))
        self.assertEqual(analytics_data["monthly"][0]["liquidBalance"], "4000.00")
        self.assertEqual(analytics_data["yearly"][0]["label"], "2026")
        self.assertEqual(analytics_data["yearly"][0]["detailUrl"], reverse("planner:projection_year_audit", args=[0]))
        self.assertEqual(analytics_data["yearly"][0]["income"], "36000.00")
        self.assertEqual(analytics_data["inflation"]["annualRate"], "2.00")
        self.assertEqual(analytics_data["monthly"][0]["liquidBalanceReal"], "4000.00")
        self.assertLess(Decimal(analytics_data["yearly"][0]["incomeReal"]), Decimal("36000.00"))
        self.assertEqual(analytics_data["yearly"][0]["netIncome"], "36000.00")
        self.assertEqual(analytics_data["yearly"][0]["taxAwareDrawNeed"], "0.00")
        self.assertEqual(analytics_data["yearly"][0]["taxAwareDrawPercent"], "0.00")
        self.assertEqual(analytics_data["milestones"]["monthly"][0]["label"], "Alex retirement starts")
        self.assertEqual(analytics_data["milestones"]["monthly"][0]["category"], "Retirement")

    def test_analytics_milestones_include_planned_investment_purchase(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("20000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        purchase = PlannedInvestmentPurchase.objects.create(
            household=household,
            name="VWRL buy",
            asset_type=PlannedInvestmentPurchase.AssetType.ETF,
            source_account=giro,
            target_account=depot,
            purchase_amount=Decimal("5000.00"),
            purchase_month=date(2026, 6, 1),
        )

        milestones = build_analytics_milestones(household)
        purchase_milestones = [item for item in milestones if item["label"] == "VWRL buy purchase"]

        self.assertEqual(len(purchase_milestones), 1)
        self.assertEqual(purchase_milestones[0]["category"], "Depot")
        self.assertEqual(purchase_milestones[0]["date"], "2026-06-01")
        self.assertEqual(purchase_milestones[0]["detail"], "5000.00 into Depot")
        self.assertEqual(
            purchase_milestones[0]["url"],
            reverse("planner:planned_investment_purchase_update", args=[purchase.pk]),
        )

    def test_analytics_balances_show_debts_by_default(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        loan_account = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        Debt.objects.create(
            household=household,
            account=loan_account,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
        )

        response = self.client.get(reverse("planner:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Debts / liabilities")
        self.assertContains(response, '"liabilityBalance": "99250.00"')
        self.assertContains(response, 'data-series="liabilityBalance" checked')
        self.assertContains(response, 'series: new Set(["liquidBalance", "netWorth", "depotValue", "liabilityBalance"])')

    def test_analytics_skips_sequence_risk_by_default(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        with patch("planner.views.build_sequence_risk_summary") as risk_summary:
            response = self.client.get(reverse("planner:analytics"))

        self.assertEqual(response.status_code, 200)
        risk_summary.assert_not_called()
        self.assertContains(response, "Run sequence risk")
        self.assertContains(response, "normal charts load without calculating 100 alternate return paths")

    def test_analytics_runs_sequence_risk_when_requested(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        fake_summary = {
            "path_count": 100,
            "annual_volatility": Decimal("15.00"),
            "success_probability": Decimal("99.00"),
            "ending_net_worth": {
                "p10": Decimal("900.00"),
                "p50": Decimal("1000.00"),
                "p90": Decimal("1100.00"),
            },
        }

        with patch("planner.views.build_sequence_risk_summary", return_value=fake_summary) as risk_summary:
            response = self.client.get(reverse("planner:analytics"), {"sequence_risk": "1"})

        self.assertEqual(response.status_code, 200)
        risk_summary.assert_called_once()
        self.assertContains(response, "100 paths")
        self.assertContains(response, "99.00%")

    def test_bond_payout_date_moves_depot_value_to_cash_and_analytics(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Target maturity bond",
            isin="IE0008UEVOE0",
            asset_class="Bond target maturity",
            quantity=Decimal("100.000000"),
            latest_price=Decimal("10.00"),
            payout_date=date(2026, 2, 15),
            payout_amount=Decimal("1100.00"),
        )

        projection = build_projection(household)
        analytics_data = build_analytics_data(projection, build_yearly_projection(projection), household)
        payout_lines = {line.section: line for line in projection[1].audit_lines}

        self.assertEqual(projection[0].liquid_balance, Decimal("500.00"))
        self.assertEqual(projection[0].invested_balance, Decimal("1000.00000000"))
        self.assertEqual(projection[1].depot_payout, Decimal("1100.00"))
        self.assertEqual(projection[1].liquid_balance, Decimal("1600.00"))
        self.assertEqual(projection[1].invested_balance, Decimal("0E-8"))
        self.assertEqual(projection[1].net_worth, Decimal("1600.00"))
        self.assertEqual(projection[1].net, Decimal("1100.00"))
        self.assertEqual(payout_lines["Depot payout"].cash_effect, Decimal("1100.00"))
        self.assertEqual(payout_lines["Depot payout"].invested_effect, Decimal("-1000.00"))
        self.assertIn("expected return since valuation 100.00", payout_lines["Depot payout"].note)
        self.assertEqual(analytics_data["monthly"][1]["depotPayout"], "1100.00")
        self.assertEqual(analytics_data["monthly"][1]["cashNet"], "1100.00")
        self.assertEqual(analytics_data["monthly"][1]["liquidBalance"], "1600.00")
        self.assertEqual(analytics_data["monthly"][1]["depotValue"], "0.00")
        milestone = next(item for item in analytics_data["milestones"]["monthly"] if item["label"] == "Target maturity bond payout")
        self.assertEqual(milestone["category"], "Depot")
        self.assertEqual(milestone["month"], "Feb 2026")
        self.assertIn("1100.00 EUR", milestone["detail"])

        response = self.client.get(reverse("planner:analytics"))

        self.assertContains(response, "Asset payouts")
        self.assertContains(response, "Target maturity bond payout")
        self.assertContains(response, '"depotPayout": "1100.00"')

    def test_depot_payout_taxes_gain_over_current_value(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Bond depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
            depot_teilfreistellung_rate=Decimal("0.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        DepotHolding.objects.create(
            asset_account=depot,
            name="Target maturity bond",
            asset_class="Bond target maturity",
            quantity=Decimal("100.000000"),
            latest_price=Decimal("10.00"),
            payout_date=date(2026, 2, 15),
            payout_amount=Decimal("1100.00"),
        )

        projection = build_projection(household)
        payout_line = next(line for line in projection[1].audit_lines if line.section == "Depot payout")

        # Only the 100 EUR gain is taxed; the 1000 EUR cost basis is returned as capital.
        self.assertEqual(projection[1].depot_payout, Decimal("1075.00"))
        self.assertEqual(projection[1].liquid_balance, Decimal("1575.00"))
        self.assertEqual(projection[1].net, Decimal("1075.00"))
        self.assertEqual(payout_line.cash_effect, Decimal("1075.00"))
        self.assertIn("100.00 taxable gain", payout_line.note)
        self.assertIn("25.00 capital tax", payout_line.note)

    def test_analytics_data_includes_tax_aware_retirement_gap(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=12,
            annual_inflation_rate=Decimal("2.00"),
            pension_tax_rate=Decimal("10.00"),
            health_insurance_rate=Decimal("10.00"),
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Statutory pension",
            current_pension_points=Decimal("10.000"),
            expected_annual_points=Decimal("1.000"),
            pension_value_per_point=Decimal("40.00"),
            retirement_start_month=date(2030, 1, 1),
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("12000.00"), start_year=2030)

        projection = build_projection(household)
        yearly_projection = build_yearly_projection(projection, household.cash_goals.all())
        analytics_data = build_analytics_data(projection, yearly_projection, household)
        retirement_year = next(point for point in analytics_data["yearly"] if Decimal(point["retirementIncome"]) > 0)

        self.assertEqual(retirement_year["netRetirementIncome"], "5376.00")
        # Gross up to net the 6,624 gap after 25% tax: 6624 / 0.75 = 8832.
        self.assertEqual(retirement_year["taxAwareDrawNeed"], "8832.00")
        self.assertEqual(retirement_year["taxAwareDrawPercent"], "0.00")

    def test_household_settings_exposes_planning_assumptions(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            annual_inflation_rate=Decimal("2.50"),
            pension_tax_rate=Decimal("17.00"),
            capital_gains_tax_rate=Decimal("25.00"),
            health_insurance_rate=Decimal("11.00"),
        )

        response = self.client.get(reverse("planner:household_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "annual_inflation_rate")
        self.assertContains(response, "default_income_growth_rate")
        self.assertContains(response, "pension_tax_rate")
        self.assertContains(response, "capital_gains_tax_rate")
        self.assertContains(response, "capital_income_allowance")
        self.assertContains(response, "vorabpauschale_basiszins_rate")
        self.assertContains(response, "health_insurance_rate")
        self.assertContains(response, "emergency_fund_months")

    def test_household_settings_saves_emergency_fund_months(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            planning_years=40,
            annual_inflation_rate=Decimal("2.50"),
            default_income_growth_rate=Decimal("1.00"),
            pension_tax_rate=Decimal("17.00"),
            income_tax_rate=Decimal("30.00"),
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("2000.00"),
            vorabpauschale_basiszins_rate=Decimal("3.20"),
            health_insurance_rate=Decimal("11.00"),
            emergency_fund_months=Decimal("0.00"),
            currency="EUR",
        )

        response = self.client.post(
            reverse("planner:household_settings"),
            {
                "name": "Test",
                "data_mode": Household.DataMode.REAL,
                "starting_balance": "1000.00",
                "start_month": "2026-01-01",
                "planning_months": "12",
                "planning_years": "40",
                "display_granularity": Household.DisplayGranularity.AUTO,
                "annual_inflation_rate": "2.50",
                "default_income_growth_rate": "1.00",
                "default_operating_account": "",
                "pension_tax_rate": "17.00",
                "income_tax_rate": "30.00",
                "capital_gains_tax_rate": "25.00",
                "capital_income_allowance": "2000.00",
                "vorabpauschale_basiszins_rate": "3.20",
                "church_tax_rate": "0.00",
                "solidarity_surcharge_rate": "0.00",
                "health_insurance_rate": "11.00",
                "fund_cash_goal_from_depot": "",
                "emergency_fund_months": "6.00",
                "currency": "EUR",
            },
        )

        self.assertRedirects(response, reverse("planner:dashboard"))
        household.refresh_from_db()
        self.assertEqual(household.emergency_fund_months, Decimal("6.00"))

    def test_household_settings_warns_when_assumptions_change(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            annual_inflation_rate=Decimal("2.00"),
        )

        response = self.client.post(
            reverse("planner:household_settings"),
            {
                "name": "Test",
                "data_mode": Household.DataMode.REAL,
                "starting_balance": "1000.00",
                "start_month": "2026-01-01",
                "planning_months": "12",
                "planning_years": "0",
                "display_granularity": Household.DisplayGranularity.AUTO,
                "annual_inflation_rate": "3.00",
                "default_income_growth_rate": "0.00",
                "default_operating_account": "",
                "pension_tax_rate": "18.00",
                "income_tax_rate": "0.00",
                "capital_gains_tax_rate": "25.00",
                "capital_income_allowance": "2000.00",
                "vorabpauschale_basiszins_rate": "3.20",
                "church_tax_rate": "0.00",
                "solidarity_surcharge_rate": "0.00",
                "health_insurance_rate": "11.00",
                "fund_cash_goal_from_depot": "",
                "emergency_fund_months": "0.00",
                "currency": "EUR",
            },
            follow=True,
        )

        self.assertContains(response, "Long-range assumptions changed")
        self.assertContains(response, "annual_inflation_rate")
        self.assertContains(response, reverse("planner:snapshots"))

    def test_dashboard_explains_emergency_fund_without_expenses(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            emergency_fund_months=Decimal("6.00"),
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "6.00 months")
        self.assertContains(response, "add recurring expenses to calculate target")

    def test_base_layout_includes_mobile_web_navigation(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="mobile-tabbar"')
        self.assertContains(response, "Accounts")
        self.assertContains(response, "Cash flow")
        self.assertContains(response, "Health")

    def test_privacy_mode_toggle_masks_rendered_money(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1234.56"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        normal_response = self.client.get(reverse("planner:dashboard"))
        self.assertContains(normal_response, "1,234.56 EUR")
        self.assertContains(normal_response, "Privacy")
        self.assertContains(normal_response, "Off")

        toggle_response = self.client.post(
            reverse("planner:privacy_mode_toggle"),
            {"enabled": "1", "next": reverse("planner:dashboard")},
            follow=True,
        )

        self.assertContains(toggle_response, "Privacy mode is now on.")
        self.assertContains(toggle_response, "••••• EUR")
        self.assertContains(toggle_response, "On")
        self.assertNotContains(toggle_response, "1,234.56 EUR")

    def test_privacy_mode_toggle_can_be_disabled(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1234.56"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        session = self.client.session
        session["privacy_mode_enabled"] = True
        session.save()

        response = self.client.post(
            reverse("planner:privacy_mode_toggle"),
            {"enabled": "0", "next": reverse("planner:dashboard")},
            follow=True,
        )

        self.assertContains(response, "Privacy mode is now off.")
        self.assertContains(response, "1,234.56 EUR")
        self.assertContains(response, "Off")
        self.assertNotContains(response, "••••• EUR")

    def test_analytics_page_renders_chart_shell(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        household = Household.objects.get()
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )

        response = self.client.get(reverse("planner:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Projection analytics")
        self.assertContains(response, "balance-chart")
        self.assertContains(response, "flow-chart")
        self.assertContains(response, "goal-chart")
        self.assertContains(response, "retirement-cashflow-chart")
        self.assertContains(response, "draw-risk-chart")
        self.assertContains(response, "ETF draw need")
        self.assertContains(response, "Tax-aware draw")
        self.assertContains(response, "Draw Risk")
        self.assertContains(response, "Sequence Risk")
        self.assertContains(response, "Run sequence risk")
        self.assertContains(response, "analytics-data")
        self.assertContains(response, "echarts.min.js")
        self.assertContains(response, "Reset zoom")

    def test_analytics_privacy_mode_masks_chart_money_formatters(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        session = self.client.session
        session["privacy_mode_enabled"] = True
        session.save()

        response = self.client.get(reverse("planner:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "const privacyMode = true;")
        self.assertContains(response, 'const maskedMoney = "••••• EUR";')
        self.assertContains(response, "••••• EUR")
        self.assertNotContains(response, "1,000.00 EUR")

    def test_goal_planner_solves_required_monthly_contribution(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            depot_annual_return_rate=Decimal("0.00"),
        )

        result = solve_monthly_contribution(
            household,
            target_net_worth=Decimal("12000.00"),
            target_month=date(2026, 12, 1),
            target_account=depot,
        )

        self.assertTrue(result.solvable)
        self.assertEqual(result.monthly_contribution, Decimal("1000.00"))
        self.assertGreaterEqual(result.ending_net_worth, Decimal("12000.00"))

    def test_goal_planner_page_renders_result(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            depot_annual_return_rate=Decimal("0.00"),
        )

        response = self.client.get(
            reverse("planner:goal_planner"),
            {
                "target_net_worth": "12000.00",
                "target_year": "2026",
                "start_month": "2026-01",
                "target_account": str(depot.pk),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Goal planner")
        self.assertContains(response, "Monthly surplus needed")
        self.assertContains(response, "1,000.00 EUR")

    def test_dashboard_shows_emergency_fund_target(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            emergency_fund_months=Decimal("3.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("5000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1000.00"),
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertContains(response, "Emergency fund target")
        self.assertContains(response, "3.00 months")
        self.assertContains(response, "3,000.00 EUR")

    def test_dashboard_has_nominal_real_display_toggle(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            annual_inflation_rate=Decimal("2.00"),
        )

        response = self.client.get(reverse("planner:dashboard"), {"display": "real"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Money display")
        self.assertContains(response, "Today's money")
        self.assertContains(response, "2.00%")

    def test_sequence_risk_summary_is_deterministic_and_reports_percentiles(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=24,
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"),
            depot_annual_return_rate=Decimal("5.00"),
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("12000.00"), start_year=2026)

        first = build_sequence_risk_summary(household, path_count=20, annual_volatility=Decimal("10.00"), seed=7)
        second = build_sequence_risk_summary(household, path_count=20, annual_volatility=Decimal("10.00"), seed=7)

        self.assertEqual(first, second)
        self.assertEqual(first["path_count"], 20)
        self.assertIn("p10", first["ending_net_worth"])
        self.assertLessEqual(first["ending_net_worth"]["p10"], first["ending_net_worth"]["p50"])
        self.assertLessEqual(first["ending_net_worth"]["p50"], first["ending_net_worth"]["p90"])
        self.assertGreaterEqual(first["success_probability"], Decimal("0.00"))
        self.assertLessEqual(first["success_probability"], Decimal("100.00"))

    def test_scenario_comparison_data_includes_base_and_active_scenarios(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        Scenario.objects.create(
            household=household,
            name="Part time",
            monthly_income_delta=Decimal("-1000.00"),
            monthly_expense_delta=Decimal("200.00"),
        )
        Scenario.objects.create(
            household=household,
            name="Disabled",
            monthly_income_delta=Decimal("-3000.00"),
            is_active=False,
        )

        comparison = build_scenario_comparison(household)

        rows = {row["label"]: row for row in comparison["rows"]}
        self.assertIn("Base plan", rows)
        self.assertIn("Stress: income -20%", rows)
        self.assertIn("Part time", rows)
        self.assertNotIn("Disabled", rows)
        self.assertEqual(rows["Base plan"]["ending_liquid"], Decimal("37000.00"))
        self.assertEqual(rows["Stress: income -20%"]["ending_liquid"], Decimal("29800.00"))
        self.assertEqual(rows["Part time"]["ending_liquid"], Decimal("22600.00"))
        self.assertEqual(rows["Base plan"]["ending_liquid_delta"], Decimal("0.00"))
        self.assertEqual(rows["Stress: income -20%"]["ending_liquid_delta"], Decimal("-7200.00"))
        self.assertEqual(rows["Part time"]["ending_net_worth_delta"], Decimal("-14400.00"))
        self.assertEqual(comparison["decision_rows"][0]["label"], "Stress: income -20%")
        self.assertEqual(comparison["preset_count"], 1)
        highlights = {item["label"]: item for item in comparison["highlights"]}
        self.assertEqual(highlights["Most liquid ending"]["winner_label"], "Base plan")
        self.assertEqual(highlights["Highest net worth"]["winner_label"], "Base plan")
        self.assertEqual(highlights["Fewest stress months"]["value"], 0)
        self.assertEqual(len(comparison["chart"]["scenarios"]), 3)
        self.assertEqual(comparison["chart"]["scenarios"][1]["label"], "Stress: income -20%")
        self.assertIn("liquidBalanceReal", comparison["chart"]["scenarios"][0])
        self.assertIn("taxAwareDrawNeed", comparison["chart"]["scenarios"][0])
        self.assertIn("taxAwareDrawPercent", comparison["chart"]["scenarios"][0])
        self.assertLess(
            Decimal(comparison["chart"]["scenarios"][0]["liquidBalanceReal"][0]),
            Decimal(comparison["chart"]["scenarios"][0]["liquidBalance"][0]),
        )

    def test_scenario_comparison_includes_depot_and_debt_stress_presets(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_annual_return_rate=Decimal("6.00"),
        )
        loan_account = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        Debt.objects.create(
            household=household,
            account=loan_account,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
        )

        comparison = build_scenario_comparison(household)
        rows = {row["label"]: row for row in comparison["rows"]}

        self.assertIn("Stress: depot return 0%", rows)
        self.assertIn("Stress: debt rates +2%", rows)
        self.assertTrue(rows["Stress: depot return 0%"]["is_preset"])
        self.assertLess(rows["Stress: depot return 0%"]["ending_depot"], rows["Base plan"]["ending_depot"])
        self.assertGreater(rows["Stress: debt rates +2%"]["ending_liability"], rows["Base plan"]["ending_liability"])

    def test_scenario_comparison_includes_estate_planning_presets(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=24,
        )
        parent = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Lina", role=Person.Role.CHILD)
        cash = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("50000.00"),
        )
        child_depot = AssetAccount.objects.create(
            household=household,
            name="Lina Kinderdepot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            counts_in_household_net_worth=False,
        )
        home = RealEstate.objects.create(
            household=household,
            name="Flat",
            current_value=Decimal("400000.00"),
            annual_appreciation_rate=Decimal("0.00"),
        )
        FamilyGiftPlan.objects.create(
            household=household,
            giver=parent,
            recipient=child,
            source_account=cash,
            target_account=child_depot,
            name="Kinderdepot gift",
            amount=Decimal("10000.00"),
            gift_month=date(2026, 2, 1),
        )
        RealEstateTransferPlan.objects.create(
            household=household,
            property_item=home,
            giver=parent,
            recipient=child,
            name="Gift flat",
            transfer_month=date(2027, 1, 1),
            ownership_percent=Decimal("100.00"),
            taxable_gift_value=Decimal("300000.00"),
        )

        comparison = build_scenario_comparison(household)
        rows = {row["label"]: row for row in comparison["rows"]}

        self.assertIn("Estate: keep property", rows)
        self.assertIn("Estate: sell property now", rows)
        self.assertIn("Estate: keep cash gifts", rows)
        self.assertIn("Estate: no estate transfers", rows)
        self.assertGreater(rows["Estate: keep property"]["ending_net_worth"], rows["Base plan"]["ending_net_worth"])
        self.assertGreater(rows["Estate: sell property now"]["ending_liquid"], rows["Base plan"]["ending_liquid"])
        self.assertGreater(rows["Estate: keep cash gifts"]["ending_liquid"], rows["Base plan"]["ending_liquid"])
        self.assertGreater(rows["Estate: no estate transfers"]["ending_net_worth"], rows["Estate: keep property"]["ending_net_worth"])
        self.assertTrue(rows["Estate: keep property"]["is_preset"])
        self.assertEqual([row["label"] for row in comparison["estate_rows"]], [
            "Estate: keep property",
            "Estate: sell property now",
            "Estate: keep cash gifts",
            "Estate: no estate transfers",
        ])

        response = self.client.get(reverse("planner:scenario_compare"))

        self.assertContains(response, "Estate Planning Comparisons")
        self.assertContains(response, "Current plans")
        self.assertContains(response, "planned property gift is paused")
        self.assertContains(response, "Converts currently owned real estate into liquid cash")
        self.assertContains(response, "Kinderdepot gifts remain in the household")
        self.assertContains(response, reverse("planner:plan_index"))
        self.assertContains(response, reverse("planner:real_estate_index"))

    def test_assumption_sensitivity_compares_key_retirement_knobs(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_years=10,
            annual_inflation_rate=Decimal("2.00"),
            fund_cash_goal_from_depot=True,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("100000.00"),
            depot_annual_return_rate=Decimal("5.00"),
        )
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Pension",
            current_pension_points=Decimal("20.000"),
            expected_annual_points=Decimal("0.000"),
            pension_value_per_point=Decimal("40.00"),
            retirement_start_month=date(2026, 1, 1),
            annual_adjustment_rate=Decimal("1.00"),
        )
        CashGoal.objects.create(
            household=household,
            name="Need",
            annual_amount=Decimal("30000.00"),
            start_year=2026,
            indexed_to_inflation=True,
        )

        groups = {group["key"]: group for group in build_assumption_sensitivity(household)}

        self.assertIn("depot_return", groups)
        self.assertIn("inflation", groups)
        self.assertIn("pension_adjustment", groups)
        self.assertIn("cash_goal", groups)
        depot_rows = {row["label"]: row for row in groups["depot_return"]["rows"]}
        cash_goal_rows = {row["label"]: row for row in groups["cash_goal"]["rows"]}
        self.assertLess(depot_rows["0% depot return"]["ending_net_worth"], depot_rows["7% depot return"]["ending_net_worth"])
        self.assertLess(
            cash_goal_rows["Cash goal -10%"]["ending_cash_goal_gap"],
            cash_goal_rows["Cash goal +10%"]["ending_cash_goal_gap"],
        )
        household.refresh_from_db()
        self.assertEqual(household.annual_inflation_rate, Decimal("2.00"))

    def test_scenario_comparison_data_includes_tax_aware_retirement_metrics(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=12,
            pension_tax_rate=Decimal("10.00"),
            health_insurance_rate=Decimal("10.00"),
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Statutory pension",
            current_pension_points=Decimal("10.000"),
            expected_annual_points=Decimal("1.000"),
            pension_value_per_point=Decimal("40.00"),
            retirement_start_month=date(2030, 1, 1),
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("12000.00"), start_year=2030)
        Scenario.objects.create(
            household=household,
            name="Lean FIRE",
            monthly_expense_delta=Decimal("-250.00"),
        )

        comparison = build_scenario_comparison(household)

        rows = {row["label"]: row for row in comparison["rows"]}
        self.assertIn("Base plan", rows)
        self.assertIn("Lean FIRE", rows)
        self.assertIn("Monthly expense adjustment", {item["label"] for item in rows["Lean FIRE"]["input_diffs"]})
        self.assertEqual(rows["Base plan"]["first_retirement_year"], "2030")
        self.assertGreater(rows["Base plan"]["ending_tax_aware_draw_need"], Decimal("0.00"))
        self.assertEqual(rows["Base plan"]["max_tax_aware_draw_percent"], Decimal("0.00"))
        self.assertEqual(rows["Base plan"]["years_above_four_percent"], 0)
        self.assertEqual(comparison["chart"]["scenarios"][0]["taxAwareDrawNeed"][4], "8832.00")
        self.assertEqual(comparison["chart"]["scenarios"][0]["annualCashGoal"][4], "12000.00")

    def test_scenario_compare_page_renders_chart_and_outcomes(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Statutory pension",
            current_pension_points=Decimal("5.000"),
            retirement_start_month=date(2026, 1, 1),
        )
        Scenario.objects.create(
            household=household,
            name="Part time",
            monthly_income_delta=Decimal("-1000.00"),
        )

        response = self.client.get(reverse("planner:scenario_compare"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scenario comparison")
        self.assertContains(response, "Base plan")
        self.assertContains(response, "Stress: income -20%")
        self.assertContains(response, "Preset stress test")
        self.assertContains(response, "Part time")
        self.assertContains(response, "scenario-chart")
        self.assertContains(response, "retirement-scenario-chart")
        self.assertContains(response, "Retirement Scenario Chart")
        self.assertContains(response, "Tax-aware draw")
        self.assertContains(response, "Years above 4%")
        self.assertContains(response, "Assumption Sensitivity")
        self.assertContains(response, "Decision Snapshot")
        self.assertContains(response, "Decision Highlights")
        self.assertContains(response, "Changed Inputs")
        self.assertContains(response, "Monthly income adjustment")
        self.assertContains(response, "-1,000.00 EUR")
        self.assertContains(response, "Most liquid ending")
        self.assertContains(response, "Highest net worth")
        self.assertContains(response, "Net worth vs base")
        self.assertContains(response, "Inflation")
        self.assertContains(response, "Retirement Health")

    def test_scenario_compare_surfaces_data_confidence(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            as_of_date=date(2026, 1, 1),
        )
        Scenario.objects.create(household=household, name="Part time", monthly_income_delta=Decimal("-1000.00"))

        response = self.client.get(reverse("planner:scenario_compare"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scenario Trust")
        self.assertContains(response, "Foundation confidence")
        self.assertContains(response, "80%")
        self.assertContains(response, "Scenario comparison is only as reliable as the account foundation")
        self.assertContains(response, "Data confidence")
        self.assertContains(response, "shared foundation")
        self.assertContains(response, reverse("planner:reconciliation_center"))

    def test_scenario_compare_privacy_mode_masks_chart_money_formatters(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        Scenario.objects.create(
            household=household,
            name="Part time",
            monthly_income_delta=Decimal("-1000.00"),
        )
        session = self.client.session
        session["privacy_mode_enabled"] = True
        session.save()

        response = self.client.get(reverse("planner:scenario_compare"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "const privacyMode = true;")
        self.assertContains(response, 'const maskedMoney = "••••• EUR";')
        self.assertContains(response, "••••• EUR")
        self.assertNotContains(response, "1,000.00 EUR")
        self.assertContains(response, "echarts.min.js")
        self.assertContains(response, "Today's money")
        self.assertContains(response, "Clone household scenario")

    def test_scenario_household_clone_creates_active_household_copy(self):
        household = Household.objects.create(
            name="Real",
            data_mode=Household.DataMode.REAL,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )

        response = self.client.get(reverse("planner:scenario_household_clone"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Clone household for a structural scenario")

        response = self.client.post(reverse("planner:scenario_household_clone"), {"name": "Scenario: bigger house"})

        self.assertRedirects(response, reverse("planner:dashboard"))
        clone = Household.objects.get(name="Scenario: bigger house")
        household.refresh_from_db()
        self.assertTrue(clone.is_active)
        self.assertFalse(household.is_active)
        self.assertEqual(clone.rules.get().name, "Salary")

    def test_feature_flags_are_created_for_admin(self):
        keys = set(FeatureFlag.objects.values_list("key", flat=True))

        self.assertTrue(set(FEATURE_FLAG_DEFINITIONS).issubset(keys))

    def test_feature_flag_uses_database_value(self):
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        self.assertTrue(feature_enabled("snapshots"))
        self.assertTrue(feature_flag_map()["snapshots"])

    def test_feature_flag_environment_override_wins(self):
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        with patch.dict("os.environ", {"LIF_FEATURE_SNAPSHOTS": "0"}):
            self.assertFalse(feature_enabled("snapshots"))
            self.assertFalse(feature_flag_map()["snapshots"])

    def test_shipped_feature_flags_default_to_enabled(self):
        enabled_by_default = {
            "analytics",
            "cash_goals",
            "depot_holdings",
            "debts",
            "income_investments",
            "retirement_plans",
            "equity_grants",
            "scenarios",
            "true_expenses",
            "child_milestones",
            "salary_changes",
            "imports",
        }
        disabled_by_default = {
            "read_only_mode",
            "snapshots",
            "moneymoney_import",
            "ynab_import",
            "multi_language",
            "advanced_tax_model",
            "docker_deployment",
            "mobile_read_only",
        }

        for key in enabled_by_default:
            with self.subTest(key=key):
                self.assertTrue(feature_enabled(key))
        for key in disabled_by_default:
            with self.subTest(key=key):
                self.assertFalse(feature_enabled(key))

    def test_feature_required_blocks_disabled_feature(self):
        request = RequestFactory().get("/")

        @feature_required("snapshots")
        def protected_view(request):
            return HttpResponse("enabled")

        with self.assertRaises(Http404):
            protected_view(request)

        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = protected_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"enabled")

    def test_disabling_shipped_feature_hides_ui_and_blocks_url(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="analytics",
            defaults={"enabled": False, "description": FEATURE_FLAG_DEFINITIONS["analytics"]["description"]},
        )
        FeatureFlag.objects.update_or_create(
            key="debts",
            defaults={"enabled": False, "description": FEATURE_FLAG_DEFINITIONS["debts"]["description"]},
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Analytics")
        self.assertNotContains(response, "Add debt")

        response = self.client.get(reverse("planner:analytics"))

        self.assertEqual(response.status_code, 404)

    def test_dashboard_surfaces_retirement_health_warnings(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=6,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        AssetAccount.objects.create(
            household=household,
            name="Checking",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Statutory pension",
            current_pension_points=Decimal("5.000"),
            pension_value_per_point=Decimal("40.00"),
            retirement_start_month=date(2028, 1, 1),
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Retirement Health")
        self.assertContains(response, "Retirement years have no cash goal")

    def test_disabling_each_gated_feature_blocks_representative_route(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        routes = [
            ("analytics", "planner:analytics"),
            ("cash_goals", "planner:cash_goal_create"),
            ("cash_goals", "planner:cash_goal_index"),
            ("depot_holdings", "planner:depot_holding_create"),
            ("depot_holdings", "planner:holding_index"),
            ("debts", "planner:debt_create"),
            ("debts", "planner:debt_index"),
            ("income_investments", "planner:income_investment_create"),
            ("income_investments", "planner:private_loan_create"),
            ("retirement_plans", "planner:retirement_plan_create"),
            ("retirement_plans", "planner:retirement_plan_index"),
            ("equity_grants", "planner:equity_grant_create"),
            ("scenarios", "planner:scenario_create"),
            ("scenarios", "planner:scenario_compare"),
            ("true_expenses", "planner:true_expense_create"),
            ("child_milestones", "planner:child_milestone_create"),
            ("salary_changes", "planner:salary_change_create"),
            ("imports", "planner:import_center"),
            ("imports", "planner:import_runbook"),
            ("imports", "planner:moneymoney_mappings"),
            ("snapshots", "planner:snapshots"),
            ("snapshots", "planner:snapshot_review"),
        ]

        for flag, route_name in routes:
            with self.subTest(flag=flag, route=route_name):
                FeatureFlag.objects.update_or_create(
                    key=flag,
                    defaults={"enabled": False, "description": FEATURE_FLAG_DEFINITIONS[flag]["description"]},
                )
                self.assertEqual(self.client.get(reverse(route_name)).status_code, 404)

    def test_disabling_action_flags_hides_dashboard_links(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        expectations = [
            ("analytics", "Analytics"),
            ("cash_goals", "Add cash goal"),
            ("depot_holdings", "Add holding"),
            ("debts", "Add debt"),
            ("income_investments", "Add investment"),
            ("retirement_plans", "Add pension"),
            ("equity_grants", "Add equity"),
            ("scenarios", "Add scenario"),
            ("imports", "Imports"),
        ]

        for flag, label in expectations:
            with self.subTest(flag=flag):
                FeatureFlag.objects.update_or_create(
                    key=flag,
                    defaults={"enabled": False, "description": FEATURE_FLAG_DEFINITIONS[flag]["description"]},
                )
                response = self.client.get(reverse("planner:dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, label)
                FeatureFlag.objects.update_or_create(
                    key=flag,
                    defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS[flag]["description"]},
                )

    def test_moneymoney_import_flag_blocks_live_connector_actions(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": False, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_accounts"})

        self.assertEqual(response.status_code, 404)

    def test_feature_flags_are_available_to_templates(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("feature_flags", response.context)
        self.assertIn("snapshots", response.context["feature_flags"])
        self.assertContains(response, "Feature Flags")
        self.assertContains(response, f"LiF {app_version()}")
        self.assertContains(response, "planner/app.js")
        self.assertContains(response, "Needs attention")

    def test_dashboard_triage_band_and_collapsible_rail(self):
        Household.objects.create(
            name="Test", starting_balance=Decimal("1000.00"), start_month=date(2026, 1, 1), planning_months=12
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        # Decision readiness + triage hub.
        self.assertContains(response, "Fix before decisions")
        self.assertContains(response, "Open checklist")
        self.assertContains(response, "Forecast headline")
        self.assertContains(response, "Top alerts")
        self.assertContains(response, "Liquidity runway")
        self.assertContains(response, "Now")
        self.assertContains(response, "Future")
        self.assertContains(response, "Forecast detail")
        self.assertContains(response, "Interactive analytics")
        # Collapsible rail controls.
        self.assertContains(response, "rail-toggle")
        self.assertContains(response, "rail-reopen")
        # Mobile shell: hamburger top bar + drawer overlay.
        self.assertContains(response, "mobile-topbar")
        self.assertContains(response, "nav-hamburger")
        self.assertContains(response, "nav-overlay")

    def test_dashboard_surfaces_account_data_confidence(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            as_of_date=date(2026, 1, 1),
        )
        review = AssumptionReview.objects.create(
            household=household,
            key="household:inflation",
            label="Inflation",
        )
        AssumptionReview.objects.filter(pk=review.pk).update(
            reviewed_at=timezone.now() - timedelta(days=400)
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data confidence")
        self.assertContains(response, "Review before decisions")
        self.assertContains(response, "Account foundation")
        self.assertContains(response, "80%")
        self.assertContains(response, "Lowest: Giro 80%")
        self.assertContains(response, "Assumption reviews")
        self.assertContains(response, "expired")
        self.assertContains(response, reverse("planner:reconciliation_center"))
        self.assertContains(response, reverse("planner:assumption_review_center"))

    def test_dashboard_shows_fire_date_when_cash_goal_is_sustainable(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=2,
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("500000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Income",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"),
        )
        CashGoal.objects.create(
            household=household,
            name="FIRE need",
            annual_amount=Decimal("30000.00"),
            start_year=2026,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FIRE date")
        self.assertContains(response, "2026")
        self.assertContains(response, "3.60% ETF draw vs 4.00%")

    def test_dashboard_fire_date_prompts_for_cash_goal(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=2,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FIRE date")
        self.assertContains(response, "No cash goal")
        self.assertContains(response, "add a yearly cash goal")

    def test_data_mode_marker_is_available_to_templates(self):
        Household.objects.create(
            name="Demo Test",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Demo data")
        self.assertContains(response, reverse("planner:real_data_readiness"))

    def test_active_household_resolver_prefers_active_household(self):
        demo = Household.objects.create(
            name="Demo",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        real = Household.objects.create(
            name="Real",
            data_mode=Household.DataMode.REAL,
            starting_balance=Decimal("2000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )

        self.assertEqual(active_household(), real)
        demo.refresh_from_db()
        self.assertFalse(demo.is_active)

    def test_active_household_resolver_marks_first_household_when_none_active(self):
        first = Household.objects.create(
            name="First",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        Household.objects.create(
            name="Second",
            starting_balance=Decimal("2000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        self.assertEqual(active_household(), first)
        first.refresh_from_db()
        self.assertTrue(first.is_active)

    def test_dashboard_uses_selected_active_household_and_switcher(self):
        demo = Household.objects.create(
            name="Demo",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        real = Household.objects.create(
            name="Real",
            data_mode=Household.DataMode.REAL,
            starting_balance=Decimal("2000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Real")
        self.assertContains(response, "Household")
        self.assertContains(response, "Demo")
        self.assertNotContains(response, "<h1>Demo</h1>", html=True)

        response = self.client.post(reverse("planner:household_switch", args=[demo.pk]))

        self.assertRedirects(response, reverse("planner:dashboard"))
        demo.refresh_from_db()
        real.refresh_from_db()
        self.assertTrue(demo.is_active)
        self.assertFalse(real.is_active)

    def test_base_layout_includes_language_switcher(self):
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'action="/i18n/setlang/"')
        self.assertContains(response, 'name="language"')
        self.assertContains(response, "German")

    def test_base_layout_uses_active_language(self):
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )

        response = self.client.get(reverse("planner:dashboard"), HTTP_ACCEPT_LANGUAGE="de")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<html lang="de">')

    def test_base_layout_translates_shared_navigation_to_german(self):
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )

        response = self.client.get(reverse("planner:dashboard"), HTTP_ACCEPT_LANGUAGE="de")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Konten")
        self.assertContains(response, "Sprache")

    def test_dashboard_translates_summary_chrome_to_german(self):
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )

        response = self.client.get(reverse("planner:dashboard"), HTTP_ACCEPT_LANGUAGE="de")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lokaler Haushaltsplaner")
        self.assertContains(response, "Vermögen jetzt")
        self.assertContains(response, "Planungshorizont")

    def test_demo_household_shows_persistent_demo_banner(self):
        Household.objects.create(
            name="Demo",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sample planning data is active")
        self.assertContains(response, "Do not enter private financial data here.")

    def test_language_can_be_changed(self):
        response = self.client.post(
            reverse("set_language"),
            {"language": "de", "next": reverse("planner:dashboard")},
        )

        self.assertRedirects(response, reverse("planner:dashboard"), fetch_redirect_response=False)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "de")
        self.assertEqual(response.cookies[settings.LIF_LANGUAGE_COOKIE_NAME].value, "de")

    def test_lif_language_cookie_overrides_accept_language(self):
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        self.client.cookies[settings.LIF_LANGUAGE_COOKIE_NAME] = "de"

        response = self.client.get(
            reverse("planner:dashboard"),
            HTTP_ACCEPT_LANGUAGE="en",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<html lang="de">')
        self.assertContains(response, "Sprache")

    def test_household_clone_copies_planning_data_and_remaps_links(self):
        source = Household.objects.create(
            name="Base plan",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=5,
            is_active=True,
        )
        adult = Person.objects.create(household=source, name="Alex", role=Person.Role.ADULT)
        child = Person.objects.create(household=source, name="Sam", role=Person.Role.CHILD)
        giro = AssetAccount.objects.create(
            household=source,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("5000.00"),
            moneymoney_account_key="bank:giro",
        )
        savings = AssetAccount.objects.create(
            household=source,
            name="Savings",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("10000.00"),
        )
        depot = AssetAccount.objects.create(
            household=source,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("20000.00"),
        )
        loan_account = AssetAccount.objects.create(
            household=source,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        source.default_operating_account = giro
        source.save(update_fields=["default_operating_account"])
        DepotHolding.objects.create(
            asset_account=depot,
            name="MSCI World",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
        )
        CashGoal.objects.create(household=source, name="Need", annual_amount=Decimal("30000.00"), start_year=2026)
        Scenario.objects.create(household=source, name="Sell property", monthly_income_delta=Decimal("100.00"))
        property_item = RealEstate.objects.create(
            household=source,
            name="Flat",
            current_value=Decimal("300000.00"),
            source_account=giro,
            sale_proceeds_account=savings,
        )
        Debt.objects.create(
            household=source,
            account=loan_account,
            source_account=giro,
            real_estate=property_item,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("900.00"),
            start_month=date(2026, 1, 1),
        )
        IncomeInvestment.objects.create(
            household=source,
            name="Solar",
            source_account=savings,
            principal=Decimal("10000.00"),
            monthly_income=Decimal("80.00"),
            start_month=date(2026, 1, 1),
            end_month=date(2030, 12, 1),
        )
        PrivateLoanReceivable.objects.create(
            household=source,
            source_account=savings,
            name="Family loan",
            current_principal=Decimal("5000.00"),
        )
        RetirementPlan.objects.create(
            household=source,
            person=adult,
            name="Pension",
            current_pension_points=Decimal("10.0000"),
            retirement_start_month=date(2050, 1, 1),
        )
        EquityGrant.objects.create(
            household=source,
            person=adult,
            account=giro,
            name="RSU",
            gross_vest_value=Decimal("1000.00"),
            first_vest_month=date(2026, 3, 1),
            last_vest_month=date(2026, 12, 1),
        )
        TrueExpense.objects.create(
            household=source,
            account=giro,
            name="Insurance",
            amount=Decimal("1200.00"),
            first_due_month=date(2026, 1, 1),
        )
        ChildMilestone.objects.create(
            person=child,
            name="School",
            start_month=date(2028, 9, 1),
            monthly_cost_delta=Decimal("100.00"),
        )
        SalaryChange.objects.create(
            person=adult,
            account=giro,
            name="Raise",
            start_month=date(2027, 1, 1),
            monthly_net_income_delta=Decimal("500.00"),
        )
        MoneyRule.objects.create(
            household=source,
            person=adult,
            account=giro,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        TransferRule.objects.create(
            household=source,
            person=adult,
            source_account=giro,
            target_account=depot,
            name="ETF top-up",
            amount=Decimal("500.00"),
        )
        PlannedInvestmentPurchase.objects.create(
            household=source,
            person=adult,
            source_account=savings,
            target_account=depot,
            name="Bond buy",
            purchase_amount=Decimal("5000.00"),
            purchase_month=date(2027, 12, 1),
        )
        FamilyGiftPlan.objects.create(
            household=source,
            giver=adult,
            recipient=child,
            source_account=savings,
            target_account=depot,
            name="Child gift",
            amount=Decimal("10000.00"),
            gift_month=date(2028, 1, 1),
        )
        ImportBatch.objects.create(household=source, source=ImportBatch.Source.MONEYMONEY)
        MoneyMoneyAccountMapping.objects.create(household=source, source_key="bank:giro", account_name="Giro")
        Snapshot.objects.create(
            household=source,
            name="Baseline",
            snapshot_date=date(2026, 1, 1),
            summary={"net_worth": "1000.00"},
        )

        response = self.client.post(
            reverse("planner:household_clone", args=[source.pk]),
            {"name": "ETF strategy"},
        )

        self.assertRedirects(response, reverse("planner:household_settings"))
        source.refresh_from_db()
        clone = Household.objects.get(name="ETF strategy")
        self.assertTrue(clone.is_active)
        self.assertFalse(source.is_active)
        self.assertEqual(clone.people.count(), 2)
        self.assertEqual(clone.accounts.count(), 4)
        self.assertEqual(clone.accounts.get(name="Depot").holdings.count(), 1)
        self.assertEqual(clone.default_operating_account.name, "Giro")
        self.assertNotEqual(clone.default_operating_account_id, giro.pk)
        self.assertEqual(clone.debts.get(name="Mortgage").account.household, clone)
        self.assertEqual(clone.debts.get(name="Mortgage").real_estate.household, clone)
        self.assertEqual(clone.rules.get(name="Salary").person.household, clone)
        self.assertEqual(clone.transfer_rules.get(name="ETF top-up").target_account.household, clone)
        self.assertEqual(clone.planned_investment_purchases.get(name="Bond buy").target_account.household, clone)
        cloned_gift = clone.family_gift_plans.get(name="Child gift")
        self.assertEqual(cloned_gift.giver.household, clone)
        self.assertEqual(cloned_gift.recipient.household, clone)
        self.assertEqual(cloned_gift.target_account.household, clone)
        self.assertEqual(clone.retirement_plans.get(name="Pension").person.household, clone)
        self.assertEqual(clone.equity_grants.get(name="RSU").account.household, clone)
        self.assertEqual(clone.true_expenses.get(name="Insurance").account.household, clone)
        self.assertEqual(clone.people.get(name="Sam").child_milestones.count(), 1)
        self.assertEqual(clone.people.get(name="Alex").salary_changes.count(), 1)
        self.assertEqual(clone.income_investments.get(name="Solar").source_account.household, clone)
        self.assertEqual(clone.private_loans.get(name="Family loan").source_account.household, clone)
        self.assertEqual(clone.import_batches.count(), 0)
        self.assertEqual(clone.moneymoney_account_mappings.count(), 0)
        self.assertEqual(clone.snapshots.count(), 0)
        self.assertEqual(clone.accounts.get(name="Giro").moneymoney_account_key, "")
        self.assertEqual(
            ChangeLogEntry.objects.filter(household=clone).exclude(model_name="Household").count(),
            0,
        )

    def test_household_settings_can_delete_inactive_household_only(self):
        active = Household.objects.create(
            name="Real",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        old_demo = Household.objects.create(
            name="Old demo",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.post(reverse("planner:household_delete", args=[active.pk]))

        self.assertRedirects(response, reverse("planner:household_settings"))
        self.assertTrue(Household.objects.filter(pk=active.pk).exists())

        response = self.client.post(reverse("planner:household_delete", args=[old_demo.pk]))

        self.assertRedirects(response, reverse("planner:household_settings"))
        self.assertFalse(Household.objects.filter(pk=old_demo.pk).exists())

    def test_mcp_uses_active_household(self):
        FeatureFlag.objects.update_or_create(
            key="mcp_server",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["mcp_server"]["description"]},
        )
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        Household.objects.create(
            name="Real",
            starting_balance=Decimal("2000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )

        overview = call_tool("overview")

        self.assertEqual(overview["household"]["name"], "Real")

    def test_sidebar_index_pages_render(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        account = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("2000.00"),
        )
        loan = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        MoneyRule.objects.create(household=household, name="Salary", kind=MoneyRule.Kind.INCOME, amount=Decimal("3000.00"))
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("30000.00"), start_year=2026)
        DepotHolding.objects.create(asset_account=depot, name="MSCI World", quantity=Decimal("10.000000"), latest_price=Decimal("100.00"))
        Debt.objects.create(
            household=household,
            account=loan,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
        )
        RetirementPlan.objects.create(
            household=household,
            person=person,
            name="Pension",
            current_pension_points=Decimal("5.000"),
            retirement_start_month=date(2050, 1, 1),
        )

        expectations = [
            ("planner:onboarding", "Setup Path"),
            ("planner:real_data_start", "Start with real data"),
            ("planner:plan_index", "Income Rules"),
            ("planner:account_index", "Current foundation"),
            ("planner:cash_goal_index", "Cash goals"),
            ("planner:holding_index", "Depot holdings"),
            ("planner:debt_index", "Debts"),
            ("planner:retirement_plan_index", "Retirement plans"),
            ("planner:assumptions_registry", "Assumptions"),
            ("planner:assumption_review_center", "Assumption review center"),
        ]
        for route_name, text in expectations:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, text)
                self.assertContains(response, "Dashboard")

    def test_attention_items_can_be_hidden_and_restored(self):
        household = Household.objects.create(
            name="Demo Test",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Demo data")
        self.assertContains(response, "Demo Tour")
        self.assertContains(response, "Real data path")
        self.assertContains(response, "Start real-data setup")
        self.assertContains(response, reverse("planner:real_data_start"))
        self.assertContains(response, "Scenarios")

        response = self.client.post(reverse("planner:attention_hide"), {"key": "demo_data"}, follow=True)

        self.assertRedirects(response, reverse("planner:dashboard"))
        self.assertContains(response, "Manage hidden items")
        self.assertContains(response, reverse("planner:attention_settings"))
        household.refresh_from_db()
        self.assertIn("demo_data", household.hidden_attention_items)

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Replace sample values before using private planning numbers.")

        response = self.client.get(reverse("planner:attention_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Attention rail")
        self.assertContains(response, "1 hidden")
        self.assertContains(response, "Hidden")
        self.assertContains(response, "Demo data")
        self.assertContains(response, "Restore")

        response = self.client.post(reverse("planner:attention_restore"), {"key": "demo_data"})

        self.assertRedirects(response, reverse("planner:attention_settings"))
        household.refresh_from_db()
        self.assertNotIn("demo_data", household.hidden_attention_items)

    def test_critical_attention_items_are_not_hidden(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.post(reverse("planner:attention_hide"), {"key": "quality_household_no-people-in-household"})

        self.assertRedirects(response, reverse("planner:dashboard"))
        household.refresh_from_db()
        self.assertEqual(household.hidden_attention_items, {})

    def test_attention_context_does_not_build_full_quality_report(self):
        from planner.context_processors import feature_flags as feature_flags_context

        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        request = RequestFactory().get("/")
        SessionMiddleware(lambda r: None).process_request(request)

        with patch("planner.quality.build_quality_report", return_value={"issues": []}) as quality_report:
            context = feature_flags_context(request)

        self.assertEqual(context["attention_count"], len(context["attention_items"]))
        quality_report.assert_not_called()

    def test_real_data_readiness_surfaces_next_checks_and_can_mark_real(self):
        household = Household.objects.create(
            name="Demo Test",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        readiness = build_household_readiness(household)
        self.assertEqual(readiness["summary"]["next_item"]["key"], "real_mode")

        response = self.client.get(reverse("planner:real_data_readiness"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Real data readiness")
        self.assertContains(response, "This household is marked as demo data")
        self.assertContains(response, "Data Completeness")
        self.assertContains(response, "Import and Reconciliation")
        self.assertContains(response, "Before You Trust the Forecast")
        self.assertContains(response, "Verify projection integrity")
        self.assertContains(response, "Reconcile starting balances")
        self.assertContains(response, "Review long-range assumptions")
        self.assertContains(response, "Compare major what-if scenarios")
        self.assertContains(response, reverse("planner:projection_integrity"))
        self.assertContains(response, reverse("planner:reconciliation_center"))
        self.assertContains(response, reverse("planner:assumptions_registry"))
        self.assertContains(response, reverse("planner:scenario_compare"))

        response = self.client.post(reverse("planner:real_data_readiness"), {"action": "mark_real"})

        self.assertRedirects(response, reverse("planner:real_data_readiness"))
        household.refresh_from_db()
        self.assertEqual(household.data_mode, Household.DataMode.REAL)

    def test_real_data_start_guides_real_instance_workflow(self):
        Household.objects.create(
            name="Demo Test",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:real_data_start"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start with real data")
        self.assertContains(response, "1. Prepare the real household")
        self.assertContains(response, "2. Enter the household foundation")
        self.assertContains(response, "3. Import and reconcile current values")
        self.assertContains(response, "4. Verify and freeze day zero")
        self.assertContains(response, "Create first real-data snapshot")
        self.assertContains(response, reverse("planner:snapshots"))

    def test_real_data_start_prompts_for_first_baseline_snapshot(self):
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )
        household = Household.objects.create(
            name="Real Test",
            data_mode=Household.DataMode.REAL,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("12000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("4000.00"),
            person=adult,
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1200.00"),
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("36000.00"), start_year=2026)
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Pension",
            current_pension_points=Decimal("10.000"),
            retirement_start_month=date(2035, 1, 1),
        )

        response = self.client.get(reverse("planner:real_data_start"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create your first real-data snapshot")
        self.assertContains(response, "Freeze this moment as day zero")

    def test_real_data_start_hides_baseline_prompt_when_snapshots_disabled(self):
        household = Household.objects.create(
            name="Real Test",
            data_mode=Household.DataMode.REAL,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("12000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("4000.00"),
            person=adult,
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("36000.00"), start_year=2026)
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Pension",
            current_pension_points=Decimal("10.000"),
            retirement_start_month=date(2035, 1, 1),
        )

        response = self.client.get(reverse("planner:real_data_start"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Create your first real-data snapshot")

    def test_first_run_setup_marks_household_as_real_data(self):
        household = Household.objects.create(
            name="Demo Test",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.post(
            reverse("planner:setup"),
            {
                "household_name": "Real household",
                "currency": "EUR",
                "start_month": "2026-01-01",
                "planning_years": "40",
                "annual_cash_goal": "30000.00",
                "adult_1_name": "Alex",
                "adult_1_birth_date": "1986-01-01",
                "adult_1_monthly_salary": "3200.00",
                "adult_2_name": "Sam",
                "adult_2_birth_date": "1986-07-01",
                "adult_2_monthly_salary": "2400.00",
                "child_1_name": "Lina",
                "child_1_birth_date": "2016-01-01",
                "child_1_kindergeld": "255.00",
                "child_2_name": "Noah",
                "child_2_birth_date": "2018-01-01",
                "child_2_kindergeld": "255.00",
            },
        )

        self.assertRedirects(response, reverse("planner:setup"))
        household.refresh_from_db()
        self.assertEqual(household.name, "Real household")
        self.assertEqual(household.data_mode, Household.DataMode.REAL)

    def test_dashboard_prompts_for_saved_annual_review_when_snapshots_are_enabled(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent changes")
        self.assertContains(response, "No saved review yet")
        self.assertContains(response, reverse("planner:snapshot_review"))

    def test_dashboard_shows_latest_saved_annual_review(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        baseline = Snapshot.objects.create(
            household=household,
            name="Start",
            snapshot_date=date(2026, 1, 1),
            summary={"household": {"currency": "EUR"}, "totals": {"net_worth": "1000.00"}},
        )
        comparison = Snapshot.objects.create(
            household=household,
            name="End",
            snapshot_date=date(2026, 12, 31),
            summary={"household": {"currency": "EUR"}, "totals": {"net_worth": "2500.00"}},
        )
        review = SnapshotReview.objects.create(
            household=household,
            baseline_snapshot=baseline,
            comparison_snapshot=comparison,
            title="2026 annual review",
            review_date=date(2027, 1, 5),
        )
        SnapshotReviewAction.objects.create(
            review=review,
            title="Review mortgage refinance",
            due_date=date(2027, 3, 31),
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["latest_review_summary"]["review"].title, "2026 annual review")
        self.assertContains(response, "2026 annual review")
        self.assertContains(response, "2027-01-05")
        self.assertContains(response, "1500.00 EUR")
        self.assertContains(response, f"{reverse('planner:snapshot_review')}?baseline={baseline.pk}&comparison={comparison.pk}")
        self.assertContains(response, "Open Review Actions")
        self.assertContains(response, "Review mortgage refinance")
        self.assertContains(response, "2027-03-31")

    def test_health_endpoint_reports_status(self):
        response = self.client.get("/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["checks"]["database"])
        self.assertIn("git_commit", response.json())

    def test_login_requirement_is_disabled_by_default(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test")

    @override_settings(LIF_REQUIRE_LOGIN=True)
    def test_login_requirement_redirects_anonymous_planner_pages(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/?next=/")

    @override_settings(LIF_REQUIRE_LOGIN=True)
    def test_login_requirement_preserves_next_query_string(self):
        response = self.client.get(f"{reverse('planner:data_quality')}?tab=warnings&kind=cash")

        redirect = urlparse(response["Location"])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(redirect.path, "/login/")
        self.assertEqual(parse_qs(redirect.query)["next"], ["/quality/?tab=warnings&kind=cash"])

    @override_settings(LIF_REQUIRE_LOGIN=True)
    def test_login_requirement_allows_health_and_login_page(self):
        health_response = self.client.get("/health/")
        login_response = self.client.get(reverse("login"))

        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, "LiF Planner")

    @override_settings(LIF_REQUIRE_LOGIN=True)
    def test_login_requirement_allows_authenticated_user(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        user = get_user_model().objects.create_user(username="local", password="secret")
        self.client.force_login(user)

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test")

    def test_system_page_renders_operational_status(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:system_status"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System")
        self.assertContains(response, f"LiF {app_version()}")
        self.assertContains(response, "Database")
        self.assertContains(response, "Feature Flags")
        self.assertContains(response, "read_only_mode")
        self.assertContains(response, "Real Data Readiness")
        self.assertContains(response, "Require app login")
        self.assertContains(response, "Create household foundation")

    def test_dashboard_redirects_empty_checkout_to_setup(self):
        response = self.client.get(reverse("planner:dashboard"))

        self.assertRedirects(response, reverse("planner:setup"), fetch_redirect_response=False)
        self.assertFalse(Household.objects.exists())

    def test_data_quality_page_surfaces_model_issues(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1200.00"),
        )

        response = self.client.get(reverse("planner:data_quality"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data quality")
        self.assertContains(response, "No accounts configured")
        self.assertContains(response, "No active income rule")
        self.assertContains(response, "Projection has negative liquidity")
        self.assertContains(response, "Focus")
        self.assertContains(response, "Projection")
        self.assertContains(response, "Projection math")
        self.assertContains(response, "Completeness Checklist")
        self.assertContains(response, "Household people")
        self.assertContains(response, "Account foundation")
        self.assertContains(response, "Assumptions reviewed")

        report = build_quality_report(household)
        checklist = {item["label"]: item for item in report["completeness"]["items"]}
        self.assertFalse(checklist["Account foundation"]["complete"])
        self.assertFalse(checklist["Recurring income"]["complete"])
        self.assertEqual(report["completeness"]["total_count"], 8)

    def test_data_health_pages_share_tab_navigation(self):
        Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=12
        )
        for url_name in ["data_quality", "reconciliation_center", "real_data_readiness", "attention_settings"]:
            response = self.client.get(reverse(f"planner:{url_name}"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'class="tab-bar"')
            # Every Data health page links to all tabs.
            self.assertContains(response, reverse("planner:data_quality"))
            self.assertContains(response, reverse("planner:reconciliation_center"))
            self.assertContains(response, reverse("planner:real_data_readiness"))
            self.assertContains(response, reverse("planner:attention_settings"))

    def test_reconciliation_center_surfaces_account_source_and_drift(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            source=AssetAccount.Source.MONEYMONEY,
            moneymoney_account_key="account:1",
            as_of_date=date(2026, 1, 1),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("500.00"),
            as_of_date=date(2026, 7, 1),
        )
        child_depot = AssetAccount.objects.create(
            household=household,
            name="Lina Kinderdepot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
            depot_valuation=AssetAccount.DepotValuation.ACCOUNT_BALANCE,
            counts_in_household_net_worth=False,
            as_of_date=date(2026, 7, 1),
        )
        DepotHolding.objects.create(
            asset_account=child_depot,
            name="World ETF",
            quantity=Decimal("12.000000"),
            latest_price=Decimal("100.00"),
        )
        MoneyRule.objects.create(household=household, name="Salary", kind=MoneyRule.Kind.INCOME, amount=Decimal("3000.00"), account=giro)
        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.MONEYMONEY,
            status=ImportBatch.Status.DRY_RUN,
            row_count=1,
            valid_count=1,
            summary={"import_kind": "moneymoney_accounts", "warning_count": 1},
        )
        MoneyMoneyAccountMapping.objects.create(
            household=household,
            source_key="account:2",
            source_kind="account",
            account_name="Old Card",
            account_type=AssetAccount.AccountType.CASH,
            import_enabled=False,
        )

        response = self.client.get(reverse("planner:reconciliation_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reconciliation")
        self.assertContains(response, "MoneyMoney selected")
        self.assertContains(response, "Warning dry-runs")
        self.assertContains(response, "Giro")
        self.assertContains(response, "Duplicate display name")
        self.assertContains(response, "Stale by")
        self.assertContains(response, "Lina Kinderdepot")
        self.assertContains(response, "Tracked outside household net worth")
        self.assertContains(response, "Depot balance differs from holdings")
        self.assertContains(response, "Review soon")
        self.assertContains(response, "Informational")
        self.assertContains(response, "2 issue(s) across 2 account(s)")
        self.assertContains(response, "3 issue(s) across 3 account(s)")
        self.assertContains(response, "Data Confidence")
        self.assertContains(response, "Average confidence")
        self.assertContains(response, "83%")
        self.assertContains(response, "High")
        self.assertContains(response, "Medium")
        self.assertContains(response, "Low")
        self.assertContains(response, "Lowest confidence account")
        self.assertContains(response, "Confidence action queue")
        self.assertContains(response, "Needs refresh")
        self.assertContains(response, "Needs source mapping")
        self.assertContains(response, "Needs holdings review")
        self.assertContains(response, "Fresh and explainable enough for normal planning.")
        self.assertContains(response, "Stale valuation -20")
        self.assertContains(response, "Depot holdings do not match account balance -20")
        self.assertContains(response, "Duplicate display name -5")
        self.assertContains(response, "Tracked outside household net worth · no score deduction")
        self.assertContains(response, "rules: 1")
        self.assertContains(response, reverse("planner:import_center"))
        self.assertContains(response, reverse("planner:moneymoney_mappings"))
        self.assertContains(response, "Depot drift · 1")
        self.assertContains(response, "MoneyMoney · 1")
        self.assertContains(response, "?focus=stale")
        self.assertContains(response, "?focus=depot_drift")
        self.assertContains(response, reverse("planner:account_update", args=[giro.pk]))
        self.assertContains(response, reverse("planner:holding_index"))
        self.assertContains(response, reverse("planner:moneymoney_mappings"))
        self.assertContains(response, "Edit account")
        self.assertContains(response, "Refresh import")
        self.assertContains(response, "Review holdings")
        self.assertContains(response, "Review mappings")

        drift_response = self.client.get(reverse("planner:reconciliation_center"), {"focus": "depot_drift"})
        self.assertContains(drift_response, "1 of 3 account(s)")
        self.assertContains(drift_response, "Lina Kinderdepot")
        self.assertNotContains(drift_response, "account:1")

        search_response = self.client.get(reverse("planner:reconciliation_center"), {"q": "account:1"})
        self.assertContains(search_response, "1 of 3 account(s)")
        self.assertContains(search_response, "account:1")
        self.assertNotContains(search_response, "Lina Kinderdepot")

    def test_reconciliation_confidence_queue_surfaces_missing_moneymoney_keys(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        household = Household.objects.get()
        AssetAccount.objects.create(
            household=household,
            name="MoneyMoney Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            source=AssetAccount.Source.MONEYMONEY,
            as_of_date=date(2026, 7, 1),
        )

        response = self.client.get(reverse("planner:reconciliation_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Needs source mapping")
        self.assertContains(response, "MoneyMoney Giro")
        self.assertContains(response, "MoneyMoney source key missing -20")
        self.assertContains(response, reverse("planner:moneymoney_mappings"))

    def test_change_history_logs_planning_model_changes(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        ChangeLogEntry.objects.all().delete()

        rule = MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        rule.amount = Decimal("3200.00")
        rule.save(update_fields=["amount"])
        rule.delete()

        entries = list(ChangeLogEntry.objects.filter(household=household).order_by("created_at"))

        self.assertEqual([entry.action for entry in entries], ["created", "updated", "deleted"])
        self.assertEqual(entries[0].model_name, "MoneyRule")
        self.assertIn("amount", entries[1].changed_fields)
        self.assertEqual(entries[1].before["amount"], "3000.00")
        self.assertEqual(entries[1].after["amount"], "3200.00")

    def test_change_history_logs_person_changes(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        ChangeLogEntry.objects.all().delete()

        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        person.name = "Alexandra"
        person.save(update_fields=["name"])

        entries = list(ChangeLogEntry.objects.filter(household=household, model_name="Person").order_by("created_at"))

        self.assertEqual([entry.action for entry in entries], ["created", "updated"])
        self.assertIn("name", entries[1].changed_fields)

    def test_change_history_page_renders_and_filters(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        entry = ChangeLogEntry.objects.create(
            household=household,
            action=ChangeLogEntry.Action.UPDATED,
            model_name="Household",
            object_pk=str(household.pk),
            object_label=household.name,
            changed_fields=["annual_inflation_rate"],
        )

        response = self.client.get(reverse("planner:change_history"), {"model": "Household", "action": "updated"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Change history")
        self.assertContains(response, "annual_inflation_rate")
        self.assertContains(response, "Household")
        self.assertContains(response, reverse("planner:change_history_detail", args=[entry.pk]))

    def test_change_history_detail_shows_before_after_values(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        entry = ChangeLogEntry.objects.create(
            household=household,
            action=ChangeLogEntry.Action.UPDATED,
            model_name="Household",
            object_pk=str(household.pk),
            object_label=household.name,
            changed_fields=["annual_inflation_rate"],
            before={"annual_inflation_rate": "2.00"},
            after={"annual_inflation_rate": "3.00"},
        )

        response = self.client.get(reverse("planner:change_history_detail", args=[entry.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Change detail")
        self.assertContains(response, "annual_inflation_rate")
        self.assertContains(response, "2.00")

    def test_change_history_detail_distinguishes_false_from_missing(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        entry = ChangeLogEntry.objects.create(
            household=household,
            action=ChangeLogEntry.Action.UPDATED,
            model_name="MoneyRule",
            object_pk="1",
            object_label="Salary",
            changed_fields=["is_active"],
            before={"is_active": True},
            after={"is_active": False},
        )

        response = self.client.get(reverse("planner:change_history_detail", args=[entry.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "True")
        self.assertContains(response, "False")

    def test_account_index_empty_state_guides_to_add(self):
        Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=12
        )

        response = self.client.get(reverse("planner:account_index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "empty-state")
        self.assertContains(response, "No accounts yet")
        self.assertContains(response, reverse("planner:account_create"))

    def test_add_holding_from_account_preselects_that_depot(self):
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=12
        )
        AssetAccount.objects.create(
            household=household, name="Depot A", account_type=AssetAccount.AccountType.DEPOT, balance=Decimal("0.00")
        )
        depot_b = AssetAccount.objects.create(
            household=household, name="Depot B", account_type=AssetAccount.AccountType.DEPOT, balance=Decimal("0.00")
        )

        response = self.client.get(f"{reverse('planner:depot_holding_create')}?account={depot_b.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial.get("asset_account"), depot_b)

    def test_add_debt_from_account_preselects_that_loan(self):
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=12
        )
        loan = AssetAccount.objects.create(
            household=household, name="Mortgage", account_type=AssetAccount.AccountType.LOAN, balance=Decimal("100000.00")
        )

        response = self.client.get(f"{reverse('planner:debt_create')}?account={loan.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial.get("account"), loan)

    def test_data_quality_page_filters_by_category_and_severity(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1200.00"),
        )

        response = self.client.get(reverse("planner:data_quality"), {"category": "Projection", "severity": "critical"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Projection has negative liquidity")
        self.assertNotIn(
            "No accounts configured",
            {item.title for item in response.context["filtered_issues"]},
        )

    def test_import_center_renders_csv_dry_run_form(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:import_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import center")
        self.assertContains(response, "Accounts CSV Dry-Run")
        self.assertContains(response, "MoneyMoney")
        self.assertContains(response, "YNAB")
        self.assertContains(response, "Reconciliation Status")
        self.assertContains(response, "No applied account import yet")

    def test_accounts_csv_dry_run_creates_import_batch_without_accounts(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        csv_file = SimpleUploadedFile(
            "accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nGiro,cash,1200.50,EUR,DKB,2026-06-25\n",
            content_type="text/csv",
        )

        response = self.client.post(reverse("planner:import_center"), {"csv_file": csv_file})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CSV dry-run completed")
        self.assertContains(response, "Giro")
        self.assertEqual(household.accounts.count(), 0)
        batch = ImportBatch.objects.get()
        self.assertEqual(batch.source, ImportBatch.Source.CSV_ACCOUNTS)
        self.assertEqual(batch.status, ImportBatch.Status.DRY_RUN)
        self.assertEqual(batch.row_count, 1)
        self.assertEqual(batch.valid_count, 1)
        self.assertEqual(batch.error_count, 0)

    def test_import_reconciliation_tracks_pending_blocked_and_account_drift(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        stale_account = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            currency="EUR",
            as_of_date=date(2000, 1, 1),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("100.00"),
            currency="EUR",
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="ETF",
            isin="IE00B3RBWM25",
            quantity=Decimal("2.000000"),
            latest_price=Decimal("75.00"),
            currency="EUR",
        )
        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="accounts.csv",
            row_count=1,
            valid_count=1,
            error_count=0,
            summary={"import_kind": "accounts", "rows": [], "missing_columns": [], "warning_count": 1},
        )
        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.FAILED,
            filename="broken.csv",
            row_count=0,
            valid_count=0,
            error_count=1,
            summary={"import_kind": "accounts", "missing_columns": ["balance"], "rows": []},
        )

        reconciliation = build_import_reconciliation(household)

        self.assertEqual(len(reconciliation["clean_pending_batches"]), 0)
        self.assertEqual(len(reconciliation["warning_pending_batches"]), 1)
        self.assertEqual(len(reconciliation["blocked_batches"]), 1)
        self.assertEqual(reconciliation["warning_pending_batches"][0].warning_count, 1)
        self.assertEqual(reconciliation["warning_pending_batches"][0].kind_label, "Accounts")
        self.assertEqual(reconciliation["next_item"]["label"], "Fix blocked imports")
        self.assertIn(stale_account, reconciliation["stale_accounts"])
        self.assertEqual(reconciliation["depot_differences"][0]["account"], depot)

        ImportBatch.objects.filter(status=ImportBatch.Status.FAILED).delete()
        reconciliation = build_import_reconciliation(household)

        self.assertEqual(reconciliation["next_item"]["label"], "Review import warnings")

    def test_clean_accounts_import_batch_can_be_applied(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        existing = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("100.00"),
            currency="EUR",
            institution="Old Bank",
            ynab_account_id="keep-me",
            notes="Keep these notes",
        )
        csv_file = SimpleUploadedFile(
            "accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nGiro,cash,1200.50,EUR,DKB,2026-06-25\nDepot,depot,50000.00,EUR,ING,2026-06-25\n",
            content_type="text/csv",
        )
        result = account_csv_dry_run(household, csv_file)
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=dry_run_summary(result),
        )

        with patch("planner.imports.call_command") as backup:
            apply_result = apply_account_import_batch(batch)

        existing.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(apply_result, {"created_count": 1, "updated_count": 1})
        self.assertEqual(existing.balance, Decimal("1200.50"))
        self.assertEqual(existing.institution, "DKB")
        self.assertEqual(existing.ynab_account_id, "keep-me")
        self.assertEqual(existing.notes, "Keep these notes")
        self.assertTrue(AssetAccount.objects.filter(household=household, name="Depot").exists())
        self.assertEqual(batch.status, ImportBatch.Status.APPLIED)
        self.assertEqual(batch.summary["apply_result"]["created_count"], 1)
        backup.assert_called_once_with("backup_data", label=f"before-import-{batch.pk}")

    def test_accounts_dry_run_reports_unchanged_and_duplicate_warnings(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1200.50"),
            currency="EUR",
            institution="DKB",
            as_of_date=date(2026, 6, 25),
        )

        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "1200.50",
                    "currency": "EUR",
                    "institution": "DKB",
                    "as_of_date": "2026-06-25",
                },
                {
                    "name": "Pocket",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "20.00",
                    "currency": "EUR",
                    "institution": "DKB",
                    "as_of_date": "2026-06-25",
                },
            ],
            start_row_number=1,
        )

        self.assertEqual(result["rows"][0].action, "unchanged")
        self.assertEqual(result["rows"][0].status, "unchanged")
        self.assertEqual(result["rows"][1].status, "warning")
        self.assertIn("Possible duplicate", result["rows"][1].warnings[0])
        self.assertEqual(result["warning_count"], 1)
        self.assertEqual(result["action_counts"]["unchanged"], 1)
        self.assertEqual(result["action_counts"]["warning"], 1)

    def test_unchanged_account_import_rows_are_not_updated(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        existing = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1200.50"),
            currency="EUR",
            institution="DKB",
            as_of_date=date(2026, 6, 25),
            notes="Do not touch",
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "1200.50",
                    "currency": "EUR",
                    "institution": "DKB",
                    "as_of_date": "2026-06-25",
                }
            ],
            start_row_number=1,
        )
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=dry_run_summary(result),
        )

        apply_result = apply_account_import_batch(batch, create_backup=False)

        existing.refresh_from_db()
        self.assertEqual(apply_result, {"created_count": 0, "updated_count": 0})
        self.assertEqual(existing.notes, "Do not touch")

    def test_import_apply_view_redirects_to_batch_detail(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        csv_file = SimpleUploadedFile(
            "accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nGiro,cash,1200.50,EUR,DKB,2026-06-25\n",
            content_type="text/csv",
        )
        result = account_csv_dry_run(household, csv_file)
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=dry_run_summary(result),
        )

        with patch("planner.imports.call_command"):
            response = self.client.post(reverse("planner:import_batch_apply", args=[batch.pk]))

        self.assertRedirects(response, reverse("planner:import_batch_detail", args=[batch.pk]))
        self.assertTrue(AssetAccount.objects.filter(household=household, name="Giro").exists())

    def test_import_apply_can_create_pre_apply_snapshot(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            data_mode=Household.DataMode.REAL,
        )
        csv_file = SimpleUploadedFile(
            "accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nGiro,cash,1200.50,EUR,DKB,2026-06-25\n",
            content_type="text/csv",
        )
        result = account_csv_dry_run(household, csv_file)
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=dry_run_summary(result),
        )

        detail = self.client.get(reverse("planner:import_batch_detail", args=[batch.pk]))
        self.assertContains(detail, 'name="create_snapshot" value="1" checked')

        with patch("planner.imports.call_command"):
            response = self.client.post(
                reverse("planner:import_batch_apply", args=[batch.pk]),
                {"create_snapshot": "1"},
            )

        self.assertRedirects(response, reverse("planner:import_batch_detail", args=[batch.pk]))
        batch.refresh_from_db()
        snapshot = household.snapshots.get()
        self.assertEqual(snapshot.name, f"Before import batch #{batch.pk}")
        self.assertEqual(snapshot.snapshot_type, Snapshot.SnapshotType.PRE_IMPORT)
        self.assertFalse(snapshot.is_baseline)
        self.assertEqual(batch.summary["apply_result"]["pre_apply_snapshot_id"], snapshot.pk)
        self.assertEqual(batch.summary["apply_result"]["pre_apply_snapshot_name"], snapshot.name)
        self.assertEqual(batch.summary["apply_result"]["backup_label"], f"before-import-{batch.pk}")

        detail = self.client.get(reverse("planner:import_batch_detail", args=[batch.pk]))
        self.assertContains(detail, "Changed Since Pre-Apply Snapshot")
        self.assertContains(detail, "Net worth")
        self.assertContains(detail, "1200.50 EUR")
        self.assertContains(detail, "Giro")
        self.assertContains(detail, "new")

    def test_import_apply_without_snapshot_leaves_snapshot_empty(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            data_mode=Household.DataMode.REAL,
        )
        csv_file = SimpleUploadedFile(
            "accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nGiro,cash,1200.50,EUR,DKB,2026-06-25\n",
            content_type="text/csv",
        )
        result = account_csv_dry_run(household, csv_file)
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=dry_run_summary(result),
        )

        with patch("planner.imports.call_command"):
            response = self.client.post(reverse("planner:import_batch_apply", args=[batch.pk]))

        self.assertRedirects(response, reverse("planner:import_batch_detail", args=[batch.pk]))
        batch.refresh_from_db()
        self.assertFalse(household.snapshots.exists())
        self.assertIsNone(batch.summary["apply_result"]["pre_apply_snapshot_id"])

    def test_import_batch_detail_shows_stored_rows_and_apply_state(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            data_mode=Household.DataMode.DEMO,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            currency="EUR",
            institution="Old Bank",
            as_of_date=date(2026, 1, 1),
        )
        csv_file = SimpleUploadedFile(
            "accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nGiro,cash,1500.00,EUR,New Bank,2026-02-01\n",
            content_type="text/csv",
        )
        result = account_csv_dry_run(household, csv_file)
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=dry_run_summary(result),
        )

        response = self.client.get(reverse("planner:import_batch_detail", args=[batch.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import batch")
        self.assertContains(response, "Clean dry-run ready to apply")
        self.assertContains(response, "Giro")
        self.assertContains(response, "Old Bank")
        self.assertContains(response, "New Bank")
        self.assertContains(response, "Apply batch")
        self.assertContains(response, "Create snapshot before apply")
        self.assertNotContains(response, 'name="create_snapshot" value="1" checked')

    def test_import_batch_detail_shows_apply_result(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "1200.50",
                    "currency": "EUR",
                    "institution": "DKB",
                    "as_of_date": "2026-06-25",
                }
            ],
            start_row_number=1,
        )
        summary = dry_run_summary(result)
        summary["import_kind"] = "accounts"
        summary["apply_result"] = {
            "created_count": 1,
            "updated_count": 0,
            "skipped_count": 0,
            "backup_label": "before-import-99",
            "pre_apply_snapshot_id": None,
            "pre_apply_snapshot_name": "",
            "applied_at": "2026-06-27T10:00:00+00:00",
        }
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.APPLIED,
            filename="accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=summary,
        )

        response = self.client.get(reverse("planner:import_batch_detail", args=[batch.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Apply Result")
        self.assertContains(response, "before-import-99")
        self.assertContains(response, "This batch has already been applied.")

    def test_import_batch_with_errors_cannot_be_applied(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        csv_file = SimpleUploadedFile(
            "bad-accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nBroken,banana,nope,EU,Bank,25.06.2026\n",
            content_type="text/csv",
        )
        result = account_csv_dry_run(household, csv_file)
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="bad-accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=dry_run_summary(result),
        )

        with self.assertRaises(ValueError):
            apply_account_import_batch(batch, create_backup=False)

        self.assertFalse(AssetAccount.objects.filter(household=household, name="Broken").exists())

    def test_applied_import_batch_cannot_be_reapplied(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.APPLIED,
            filename="accounts.csv",
            row_count=0,
            valid_count=0,
            error_count=0,
            summary={"rows": [], "missing_columns": []},
        )

        with self.assertRaises(ValueError):
            apply_account_import_batch(batch, create_backup=False)

    def test_depot_holdings_csv_dry_run_creates_import_batch_without_holdings(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            currency="EUR",
        )
        csv_file = SimpleUploadedFile(
            "holdings.csv",
            (
                b"account_name,name,isin,ticker,asset_class,quantity,latest_price,currency,as_of_date,payout_date\n"
                b"ING Depot,Vanguard FTSE All-World,IE00B3RBWM25,VGWL,ETF distributing,10.500000,118.42,EUR,2026-06-25,\n"
            ),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("planner:import_center"),
            {"import_kind": "depot_holdings", "csv_file": csv_file},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Depot holdings CSV dry-run completed")
        self.assertContains(response, "Vanguard FTSE All-World")
        self.assertEqual(DepotHolding.objects.count(), 0)
        batch = ImportBatch.objects.get()
        self.assertEqual(batch.source, ImportBatch.Source.CSV_DEPOT_HOLDINGS)
        self.assertEqual(batch.status, ImportBatch.Status.DRY_RUN)
        self.assertEqual(batch.row_count, 1)
        self.assertEqual(batch.valid_count, 1)
        self.assertEqual(batch.error_count, 0)

    def test_clean_depot_holdings_import_batch_can_be_applied(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            currency="EUR",
        )
        existing = DepotHolding.objects.create(
            asset_account=depot,
            name="Vanguard FTSE All-World",
            isin="IE00B3RBWM25",
            ticker="VGWL",
            asset_class="ETF distributing",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
            currency="EUR",
            notes="Keep these notes",
        )
        csv_file = SimpleUploadedFile(
            "holdings.csv",
            (
                b"account_name,name,isin,ticker,asset_class,quantity,latest_price,currency,as_of_date,payout_date,payout_amount\n"
                b"ING Depot,Vanguard FTSE All-World,IE00B3RBWM25,VGWL,ETF distributing,12.500000,118.42,EUR,2026-06-25,,\n"
                b"ING Depot,German Government Bond 2031,IE0008UEVOE0,,Bond,20.000000,99.10,EUR,2026-06-25,2031-12-15,30000.00\n"
            ),
            content_type="text/csv",
        )
        result = depot_holding_csv_dry_run(household, csv_file)
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_DEPOT_HOLDINGS,
            status=ImportBatch.Status.DRY_RUN,
            filename="holdings.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=dry_run_summary(result),
        )

        with patch("planner.imports.call_command") as backup:
            apply_result = apply_depot_holding_import_batch(batch)

        existing.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(apply_result, {"created_count": 1, "updated_count": 1})
        self.assertEqual(existing.quantity, Decimal("12.500000"))
        self.assertEqual(existing.latest_price, Decimal("118.42"))
        self.assertEqual(existing.notes, "Keep these notes")
        imported_bond = DepotHolding.objects.get(asset_account=depot, isin="IE0008UEVOE0")
        self.assertEqual(imported_bond.payout_date, date(2031, 12, 15))
        self.assertEqual(imported_bond.payout_amount, Decimal("30000.00"))
        self.assertEqual(batch.status, ImportBatch.Status.APPLIED)
        self.assertEqual(batch.summary["apply_result"]["created_count"], 1)
        backup.assert_called_once_with("backup_data", label=f"before-import-{batch.pk}")

    def test_depot_holdings_dry_run_reports_unchanged_and_isin_warnings(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        primary = AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            currency="EUR",
        )
        other = AssetAccount.objects.create(
            household=household,
            name="DKB Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            currency="EUR",
        )
        AssetAccount.objects.create(
            household=household,
            name="New Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            currency="EUR",
        )
        DepotHolding.objects.create(
            asset_account=primary,
            name="Vanguard FTSE All-World",
            isin="IE00B3RBWM25",
            ticker="VGWL",
            asset_class="ETF distributing",
            quantity=Decimal("10.500000"),
            latest_price=Decimal("118.42"),
            currency="EUR",
            as_of_date=date(2026, 6, 25),
        )
        DepotHolding.objects.create(
            asset_account=other,
            name="Vanguard FTSE All-World",
            isin="IE00B3RBWM25",
            ticker="",
            asset_class="ETF distributing",
            quantity=Decimal("1.000000"),
            latest_price=Decimal("118.42"),
            currency="EUR",
        )
        csv_file = SimpleUploadedFile(
            "holdings.csv",
            (
                b"account_name,name,isin,ticker,asset_class,quantity,latest_price,currency,as_of_date,payout_date\n"
                b"ING Depot,Vanguard FTSE All-World,IE00B3RBWM25,VGWL,ETF distributing,10.500000,118.42,EUR,2026-06-25,\n"
                b"New Depot,Vanguard FTSE All-World,IE00B3RBWM25,,ETF distributing,2.000000,118.42,EUR,2026-06-25,\n"
            ),
            content_type="text/csv",
        )

        result = depot_holding_csv_dry_run(household, csv_file)

        self.assertEqual(result["rows"][0].action, "unchanged")
        self.assertEqual(result["rows"][0].status, "unchanged")
        self.assertEqual(result["rows"][1].status, "warning")
        self.assertIn("Same ISIN already exists in another depot", result["rows"][1].warnings[0])
        self.assertEqual(result["action_counts"]["unchanged"], 1)
        self.assertEqual(result["action_counts"]["warning"], 1)

    def test_depot_holdings_csv_dry_run_reports_validation_errors(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Cash Account",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("0.00"),
            currency="EUR",
        )
        csv_file = SimpleUploadedFile(
            "bad-holdings.csv",
            (
                b"account_name,name,isin,ticker,asset_class,quantity,latest_price,currency,as_of_date,payout_date\n"
                b"Cash Account,,IE00B3RBWM25,VGWL,ETF,nope,-1,EU,25.06.2026,31.12.2031\n"
            ),
            content_type="text/csv",
        )

        result = depot_holding_csv_dry_run(household, csv_file)

        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["valid_count"], 0)
        self.assertEqual(result["error_count"], 1)
        self.assertIn("Depot account must already exist and have account type depot.", result["rows"][0].errors)
        self.assertIn("Holding name is required.", result["rows"][0].errors)
        self.assertIn("Quantity must be a decimal number.", result["rows"][0].errors)
        self.assertIn("Latest price cannot be negative.", result["rows"][0].errors)
        self.assertIn("Currency must be a three-letter code.", result["rows"][0].errors)
        self.assertIn("As-of date must use YYYY-MM-DD.", result["rows"][0].errors)
        self.assertIn("Payout date must use YYYY-MM-DD.", result["rows"][0].errors)

    def test_accounts_csv_dry_run_reports_validation_errors(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        csv_file = SimpleUploadedFile(
            "bad-accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nBroken,banana,nope,EU,Bank,25.06.2026\n",
            content_type="text/csv",
        )

        result = account_csv_dry_run(household, csv_file)

        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["valid_count"], 0)
        self.assertEqual(result["error_count"], 1)
        self.assertIn("Account type must be one of cash, savings, depot, loan, other.", result["rows"][0].errors)
        self.assertIn("Balance must be a decimal number.", result["rows"][0].errors)
        self.assertIn("Currency must be a three-letter code.", result["rows"][0].errors)
        self.assertIn("As-of date must use YYYY-MM-DD.", result["rows"][0].errors)

    def test_import_center_can_be_disabled(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="imports",
            defaults={"enabled": False, "description": FEATURE_FLAG_DEFINITIONS["imports"]["description"]},
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Imports")
        self.assertEqual(self.client.get(reverse("planner:import_center")).status_code, 404)

    def test_snapshots_are_hidden_until_feature_enabled(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        snapshot = Snapshot.objects.create(
            household=household,
            name="Hidden baseline",
            snapshot_date=date(2026, 1, 1),
            summary={},
        )

        response = self.client.get(reverse("planner:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Snapshots")
        self.assertEqual(self.client.get(reverse("planner:snapshots")).status_code, 404)
        self.assertEqual(self.client.get(reverse("planner:snapshot_compare", args=[snapshot.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("planner:snapshot_projection_changes")).status_code, 404)

    def test_snapshot_summary_freezes_current_baseline(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
            currency="EUR",
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="ETF",
            isin="IE00B3RBWM25",
            asset_class="ETF",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
            currency="EUR",
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
            currency="EUR",
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        CashGoal.objects.create(household=household, name="FIRE", annual_amount=Decimal("36000.00"), start_year=2026)
        PrivateLoanReceivable.objects.create(
            household=household,
            name="Family loan",
            borrower="Sibling",
            current_principal=Decimal("5000.00"),
            monthly_interest_income=Decimal("25.00"),
            monthly_principal_repayment=Decimal("250.00"),
        )
        giro = household.accounts.get(name="Giro")
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="Future bond",
            asset_type=PlannedInvestmentPurchase.AssetType.BOND,
            source_account=giro,
            target_account=depot,
            purchase_amount=Decimal("28450.00"),
            purchase_month=date(2027, 12, 1),
            payout_date=date(2029, 1, 1),
            payout_amount=Decimal("30000.00"),
        )

        summary = build_snapshot_summary(household)

        self.assertEqual(summary["totals"]["liquid"], "500.00")
        self.assertEqual(summary["totals"]["invested"], "1000.00")
        self.assertEqual(summary["totals"]["other_assets"], "5000.00")
        self.assertEqual(summary["totals"]["net_worth"], "6500.00")
        self.assertEqual(summary["counts"]["accounts"], 2)
        self.assertEqual(summary["counts"]["holdings"], 1)
        self.assertEqual(summary["counts"]["private_loans"], 1)
        self.assertEqual(summary["counts"]["planned_investment_purchases"], 1)
        self.assertEqual(summary["private_loans"][0]["name"], "Family loan")
        self.assertEqual(summary["planned_investment_purchases"][0]["name"], "Future bond")
        self.assertEqual(summary["planned_investment_purchases"][0]["purchase_month"], "2027-12-01")
        self.assertEqual(summary["planned_investment_purchases"][0]["payout_amount"], "30000.00")
        self.assertEqual(summary["rules"][0]["name"], "Salary")
        self.assertEqual(summary["projection"]["monthly"][0]["label"], "Jan 2026")
        self.assertEqual(summary["projection"]["yearly"][0]["label"], "2026")

    def test_snapshot_page_creates_frozen_snapshot(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
            currency="EUR",
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.post(
            reverse("planner:snapshots"),
            {
                "name": "Before import",
                "snapshot_type": Snapshot.SnapshotType.MANUAL,
                "snapshot_date": "2026-06-25",
                "notes": "Baseline",
            },
        )

        snapshot = Snapshot.objects.get()
        self.assertRedirects(response, reverse("planner:snapshot_detail", args=[snapshot.pk]))
        self.assertEqual(snapshot.summary["totals"]["liquid"], "500.00")
        account.balance = Decimal("900.00")
        account.save()
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.summary["totals"]["liquid"], "500.00")

        detail = self.client.get(reverse("planner:snapshot_detail", args=[snapshot.pk]))
        self.assertContains(detail, "Before import")
        self.assertContains(detail, "500.00 EUR")

    def test_snapshot_comparison_detects_current_changes(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            currency="EUR",
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        holding = DepotHolding.objects.create(
            asset_account=depot,
            name="MSCI World",
            isin="IE00B3RBWM25",
            asset_class="ETF",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
            currency="EUR",
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
            currency="EUR",
        )
        loan = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
            currency="EUR",
        )
        debt = Debt.objects.create(
            household=household,
            account=loan,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"),
        )
        private_loan = PrivateLoanReceivable.objects.create(
            household=household,
            name="Family loan",
            borrower="Sibling",
            current_principal=Decimal("5000.00"),
            monthly_interest_income=Decimal("25.00"),
            monthly_principal_repayment=Decimal("250.00"),
        )
        snapshot = Snapshot.objects.create(
            household=household,
            name="Before cleanup",
            snapshot_date=date(2026, 6, 25),
            summary=build_snapshot_summary(household),
        )

        giro.balance = Decimal("750.00")
        giro.save()
        holding.latest_price = Decimal("90.00")
        holding.save()
        debt.current_principal = Decimal("99000.00")
        debt.save()
        private_loan.current_principal = Decimal("4500.00")
        private_loan.save()
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("2000.00"),
            currency="EUR",
        )
        current_summary = build_snapshot_summary(household)

        comparison = compare_snapshot_to_current(snapshot.summary, current_summary)

        totals = {row["key"]: row for row in comparison["totals"]}
        self.assertEqual(totals["liquid"]["snapshot_value"], "500.00")
        self.assertEqual(totals["liquid"]["current_value"], "2750.00")
        self.assertEqual(totals["liquid"]["delta"], "2250.00")
        accounts = {row["label"]: row for row in comparison["accounts"]}
        self.assertEqual(accounts["Giro"]["status"], "changed")
        self.assertEqual(accounts["Tagesgeld"]["status"], "new")
        holdings = {row["label"]: row for row in comparison["holdings"]}
        self.assertEqual(holdings["MSCI World"]["status"], "changed")
        self.assertEqual(holdings["MSCI World"]["delta"], "-100.00")
        debts = {row["label"]: row for row in comparison["debts"]}
        self.assertEqual(debts["Mortgage"]["status"], "changed")
        self.assertEqual(debts["Mortgage"]["delta"], "-1000.00")
        private_loans = {row["label"]: row for row in comparison["private_loans"]}
        self.assertEqual(private_loans["Family loan"]["status"], "changed")
        self.assertEqual(private_loans["Family loan"]["delta"], "-500.00")
        self.assertIn("planned_current_month", comparison)

    def test_snapshot_summary_comparison_compares_two_frozen_snapshots(self):
        baseline = {
            "household": {"currency": "EUR"},
            "totals": {"liquid": "1000.00", "invested": "5000.00", "net_worth": "6000.00"},
            "accounts": [{"name": "Giro", "effective_balance": "1000.00", "type": "cash"}],
            "holdings": [{"name": "ETF", "isin": "IE00B3RBWM25", "current_value": "5000.00"}],
        }
        comparison = {
            "household": {"currency": "EUR"},
            "totals": {"liquid": "1500.00", "invested": "5500.00", "net_worth": "7000.00"},
            "accounts": [
                {"name": "Giro", "effective_balance": "1500.00", "type": "cash"},
                {"name": "Tagesgeld", "effective_balance": "2000.00", "type": "savings"},
            ],
            "holdings": [{"name": "ETF", "isin": "IE00B3RBWM25", "current_value": "5500.00"}],
        }

        review = compare_snapshot_summaries(baseline, comparison)

        totals = {row["key"]: row for row in review["totals"]}
        self.assertEqual(totals["net_worth"]["snapshot_value"], "6000.00")
        self.assertEqual(totals["net_worth"]["current_value"], "7000.00")
        self.assertEqual(totals["net_worth"]["delta"], "1000.00")
        accounts = {row["label"]: row for row in review["accounts"]}
        self.assertEqual(accounts["Giro"]["status"], "changed")
        self.assertEqual(accounts["Tagesgeld"]["status"], "new")

    def test_snapshot_comparison_keeps_duplicate_named_accounts_distinct(self):
        baseline = {
            "household": {"currency": "EUR"},
            "totals": {},
            "accounts": [
                {"name": "Giro", "effective_balance": "100.00", "type": "cash", "source_key": "account:7"},
                {"name": "Giro", "effective_balance": "200.00", "type": "cash", "source_key": "account:8"},
            ],
            "holdings": [],
        }
        comparison = {
            "household": {"currency": "EUR"},
            "totals": {},
            "accounts": [
                {"name": "Giro", "effective_balance": "150.00", "type": "cash", "source_key": "account:7"},
                {"name": "Giro", "effective_balance": "250.00", "type": "cash", "source_key": "account:8"},
            ],
            "holdings": [],
        }

        review = compare_snapshot_summaries(baseline, comparison)

        # Two distinct accounts (not collapsed to one), each up 50.
        self.assertEqual(len(review["accounts"]), 2)
        self.assertEqual(sorted(row["delta"] for row in review["accounts"]), ["50.00", "50.00"])

    def test_snapshot_comparison_keeps_same_isin_across_accounts_distinct(self):
        baseline = {
            "household": {"currency": "EUR"},
            "totals": {},
            "accounts": [],
            "holdings": [
                {"account": "Depot A", "name": "World", "isin": "IE00B3RBWM25", "current_value": "1000.00"},
                {"account": "Depot B", "name": "World", "isin": "IE00B3RBWM25", "current_value": "2000.00"},
            ],
        }
        comparison = {
            "household": {"currency": "EUR"},
            "totals": {},
            "accounts": [],
            "holdings": [
                {"account": "Depot A", "name": "World", "isin": "IE00B3RBWM25", "current_value": "1100.00"},
                {"account": "Depot B", "name": "World", "isin": "IE00B3RBWM25", "current_value": "2200.00"},
            ],
        }

        review = compare_snapshot_summaries(baseline, comparison)

        # Same ISIN in two depots stays two rows.
        self.assertEqual(len(review["holdings"]), 2)
        self.assertEqual(sorted(row["delta"] for row in review["holdings"]), ["100.00", "200.00"])

    def test_snapshot_summary_is_versioned_with_stable_identity_keys(self):
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=12
        )
        account = AssetAccount.objects.create(
            household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("500.00")
        )

        summary = build_snapshot_summary(household)

        self.assertEqual(summary["schema_version"], 2)
        self.assertEqual(summary["accounts"][0]["key"], f"account:{account.pk}")

    def test_snapshot_comparison_matches_renamed_account_by_identity(self):
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=12
        )
        giro = AssetAccount.objects.create(
            household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("500.00")
        )
        baseline = build_snapshot_summary(household)

        giro.name = "Main checking"
        giro.balance = Decimal("700.00")
        giro.save()
        current = build_snapshot_summary(household)

        review = compare_snapshot_summaries(baseline, current)

        # Renamed account is the SAME row (matched by PK), not a missing + a new.
        self.assertEqual(len(review["accounts"]), 1)
        self.assertEqual(review["accounts"][0]["status"], "changed")
        self.assertEqual(review["accounts"][0]["delta"], "200.00")

    def test_snapshot_comparison_falls_back_for_legacy_keyless_summaries(self):
        # v1 summaries (no schema_version, no per-row key) still diff by name.
        baseline = {"accounts": [{"name": "Giro", "effective_balance": "100.00"}]}
        current = {"accounts": [{"name": "Giro", "effective_balance": "150.00"}]}

        review = compare_snapshot_summaries(baseline, current)

        self.assertEqual(review["schema_version"], 1)
        self.assertEqual(len(review["accounts"]), 1)
        self.assertEqual(review["accounts"][0]["status"], "changed")
        self.assertEqual(review["accounts"][0]["delta"], "50.00")

    def test_scenario_stress_months_excludes_insolvent_months(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        MoneyRule.objects.create(
            household=household,
            name="Big bill",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("100.00"),
        )

        comparison = build_scenario_comparison(household)
        base = comparison["rows"][0]

        # The only month is cash-negative but also insolvent (net worth < 0), so
        # it is not counted as cash stress -- matching the liquidity view.
        self.assertEqual(base["stress_months"], 0)

    def test_yearly_real_flows_discount_at_year_midpoint(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_years=1,
            annual_inflation_rate=Decimal("10.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Income",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"),
        )
        projection = build_projection(household)
        yearly = build_yearly_projection(build_projection(household), household.cash_goals.all())

        data = build_analytics_data(projection, yearly, household)

        self.assertEqual(data["yearly"][0]["income"], "12000.00")
        # Discounted at the 6-month midpoint, not the 12-month year end.
        self.assertEqual(
            data["yearly"][0]["incomeReal"],
            money_value(real_value(Decimal("12000.00"), Decimal("10.00"), 6)),
        )
        self.assertNotEqual(
            data["yearly"][0]["incomeReal"],
            money_value(real_value(Decimal("12000.00"), Decimal("10.00"), 12)),
        )

    def test_money_value_rounds_half_up(self):
        self.assertEqual(money_value(Decimal("0.005")), "0.01")

    def test_income_timeline_reconciles_and_includes_income_rules(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_years=1,
        )
        # Salary as an income rule -- the source that had no breakdown field.
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
            start_month=date(2026, 1, 1),
        )
        CashGoal.objects.create(household=household, name="FIRE need", annual_amount=Decimal("30000.00"), start_year=2026)
        AssetAccount.objects.create(
            household=household, name="Tagesgeld", account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"), savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.MONTHLY, savings_interest_tax_rate=Decimal("25.00"),
        )

        timeline = build_income_timeline(household)
        projection = build_projection(household)
        year = build_yearly_projection(projection, household.cash_goals.all())[0]

        # Salary (income rule) is now a visible column, and every row reconciles.
        self.assertIn("Income rule", timeline["columns"])
        self.assertIn("Savings interest", timeline["columns"])
        self.assertTrue(timeline["all_reconcile"])
        # The single year's per-source values sum to the projection's income.
        row = timeline["rows"][0]
        self.assertEqual(row["total"], year.income)
        self.assertEqual(sum(row["values"]), year.income)
        self.assertIn("Salary income starts", [event["label"] for event in row["events"]])
        self.assertIn("FIRE need cash goal starts", [event["label"] for event in row["events"]])
        # Salary contributes 12 x 3000.
        salary_idx = timeline["columns"].index("Income rule")
        self.assertEqual(row["values"][salary_idx], Decimal("36000.00"))

    def test_yearly_audit_links_track_bucket_position(self):
        # With calendar-year buckets a mid-year start makes a partial first year, so
        # the audit link must use the row's POSITION, not the old start_index // 12
        # (which collapsed the partial 2026 and full 2027 onto the same index).
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 7, 1), planning_years=2
        )
        MoneyRule.objects.create(
            household=household, name="Salary", kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"), start_month=date(2026, 7, 1),
        )

        rows = build_income_timeline(household)["rows"]

        self.assertEqual([r["label"] for r in rows], ["2026 (6 mo)", "2027", "2028 (6 mo)"])
        self.assertEqual(rows[1]["detail_url"], reverse("planner:projection_year_audit", args=[1]))
        self.assertEqual(rows[2]["detail_url"], reverse("planner:projection_year_audit", args=[2]))

    def test_depot_distribution_income_is_net_of_capital_tax(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("12000.00"), depot_annual_return_rate=Decimal("0.00"),
            depot_annual_distribution_rate=Decimal("12.00"),
            depot_teilfreistellung_rate=Decimal("0.00"),
            depot_distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
        )

        projection = build_projection(household)

        # 12000 x 12%/12 = 120 gross, 25% capital tax -> 90 net distribution.
        self.assertEqual(projection[0].depot_income, Decimal("90.00"))
        self.assertEqual(projection[0].income, Decimal("90.00"))
        # Paid as cash; the depot value is not reduced.
        self.assertEqual(projection[0].invested_balance, Decimal("12000.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("90.00"))
        lines = {line.section for line in projection[0].audit_lines}
        self.assertIn("Depot distribution", lines)

    def test_holdings_valued_depot_distributes_per_holding_not_per_account(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("20000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
            depot_annual_return_rate=Decimal("0.00"),
            # Leftover account-level rate must be ignored for holdings-valued depots.
            depot_annual_distribution_rate=Decimal("99.00"),
            depot_distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
            depot_teilfreistellung_rate=Decimal("0.00"),
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Accumulating ETF",
            quantity=Decimal("100.000000"),
            latest_price=Decimal("100.00"),
            annual_distribution_rate=Decimal("0.00"),
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Distributing ETF",
            quantity=Decimal("100.000000"),
            latest_price=Decimal("100.00"),
            annual_distribution_rate=Decimal("12.00"),
            distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
        )

        projection = build_projection(household)

        # Only the distributing holding pays out: 10000 x 12%/12 = 100 gross,
        # 25% tax -> 75 net. The accumulating holding contributes nothing, and
        # the account-level 99% rate is ignored entirely for this valuation mode.
        self.assertEqual(projection[0].depot_income, Decimal("75.00"))
        distribution_lines = [line for line in projection[0].audit_lines if line.section == "Depot distribution"]
        self.assertEqual(len(distribution_lines), 1)
        self.assertEqual(distribution_lines[0].name, "Distributing ETF")

    def test_holding_distribution_scales_with_depot_growth(self):
        def build(annual_return_rate):
            household = Household.objects.create(
                name="Test",
                starting_balance=Decimal("0.00"),
                start_month=date(2026, 1, 1),
                planning_months=13,
                capital_gains_tax_rate=Decimal("0.00"),
                church_tax_rate=Decimal("0.00"),
                solidarity_surcharge_rate=Decimal("0.00"),
            )
            depot = AssetAccount.objects.create(
                household=household,
                name="Depot",
                account_type=AssetAccount.AccountType.DEPOT,
                balance=Decimal("1000.00"),
                depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
                depot_annual_return_rate=annual_return_rate,
            )
            DepotHolding.objects.create(
                asset_account=depot,
                name="Distributing ETF",
                quantity=Decimal("10.000000"),
                latest_price=Decimal("100.00"),
                annual_distribution_rate=Decimal("6.00"),
                distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
            )
            return build_projection(household)

        flat = build(Decimal("0.00"))
        growing = build(Decimal("12.00"))

        # With no growth, the flat-rate distribution never changes, matching
        # the depot's static starting value every month.
        self.assertEqual(flat[0].depot_income, flat[12].depot_income)

        # With growth, twelve further months of compounding pay out
        # proportionally more than the same holding did earlier, tracking the
        # depot's growth instead of paying the same flat amount forever.
        self.assertGreater(growing[12].depot_income, growing[0].depot_income)
        self.assertGreater(growing[0].depot_income, flat[0].depot_income)

    def test_planned_investment_purchase_distributes_from_its_own_rate(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
            capital_gains_tax_rate=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
            depot_annual_return_rate=Decimal("0.00"),
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Accumulating ETF",
            quantity=Decimal("100.000000"),
            latest_price=Decimal("100.00"),
            annual_distribution_rate=Decimal("0.00"),
        )
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="Distributing ETF purchase",
            asset_type=PlannedInvestmentPurchase.AssetType.ETF,
            target_account=depot,
            purchase_amount=Decimal("2000.00"),
            purchase_month=date(2026, 2, 1),
            annual_distribution_rate=Decimal("12.00"),
            distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
        )

        projection = build_projection(household)

        # Before the purchase happens there is nothing to distribute from --
        # the only existing holding is accumulating (0% rate).
        self.assertEqual(projection[0].depot_income, Decimal("0.00"))
        # From the purchase's own first month onward: 2000 x 12%/12 = 20
        # gross, no tax configured -> 20 net, using the purchase's own rate,
        # not the account's or any other holding's.
        self.assertEqual(projection[1].depot_income, Decimal("20.00"))
        self.assertEqual(projection[2].depot_income, Decimal("20.00"))
        distribution_lines = [line for line in projection[1].audit_lines if line.section == "Depot distribution"]
        self.assertEqual(len(distribution_lines), 1)
        self.assertEqual(distribution_lines[0].name, "Distributing ETF purchase")

    def test_planned_investment_purchase_distribution_grows_with_depot_return(self):
        def build(annual_return_rate):
            household = Household.objects.create(
                name="Test",
                starting_balance=Decimal("0.00"),
                start_month=date(2026, 1, 1),
                planning_months=13,
                capital_gains_tax_rate=Decimal("0.00"),
                church_tax_rate=Decimal("0.00"),
                solidarity_surcharge_rate=Decimal("0.00"),
            )
            depot = AssetAccount.objects.create(
                household=household,
                name="Depot",
                account_type=AssetAccount.AccountType.DEPOT,
                balance=Decimal("0.00"),
                depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
                depot_annual_return_rate=annual_return_rate,
            )
            PlannedInvestmentPurchase.objects.create(
                household=household,
                name="Distributing ETF purchase",
                asset_type=PlannedInvestmentPurchase.AssetType.ETF,
                target_account=depot,
                purchase_amount=Decimal("1000.00"),
                purchase_month=date(2026, 1, 1),
                annual_distribution_rate=Decimal("6.00"),
                distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
            )
            return build_projection(household)

        flat = build(Decimal("0.00"))
        growing = build(Decimal("12.00"))

        # With no depot return, the purchase's own distribution base never changes.
        self.assertEqual(flat[0].depot_income, flat[12].depot_income)

        # The purchase starts fresh at its own purchase amount -- unlike a
        # pre-existing holding (which is already a month into compounding the
        # first time a projection observes it), a brand new purchase has had
        # no growth yet in its own first month, regardless of the account's
        # return rate.
        self.assertEqual(growing[0].depot_income, flat[0].depot_income)

        # After twelve further months of compounding, the grown value pays
        # out more, tracking the depot's own return rate instead of paying
        # the same flat amount forever.
        self.assertGreater(growing[12].depot_income, growing[0].depot_income)

    def test_quality_report_flags_double_count_from_purchase_level_distribution(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="Distributing ETF purchase",
            asset_type=PlannedInvestmentPurchase.AssetType.ETF,
            target_account=depot,
            purchase_amount=Decimal("2000.00"),
            purchase_month=date(2026, 2, 1),
            annual_distribution_rate=Decimal("2.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Manual ETF distributions",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("50.00"),
            category="Investment distributions",
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Depot distributions may be double-counted", titles)

    def test_holding_distributions_reduce_vorabpauschale_to_zero(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=13,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            vorabpauschale_basiszins_rate=Decimal("3.20"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Mixed depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
            depot_annual_return_rate=Decimal("0.00"),
            depot_teilfreistellung_rate=Decimal("30.00"),
            depot_vorabpauschale_enabled=True,
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Accumulating half",
            quantity=Decimal("50.000000"),
            latest_price=Decimal("100.00"),
            annual_distribution_rate=Decimal("0.00"),
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Distributing half",
            quantity=Decimal("50.000000"),
            latest_price=Decimal("100.00"),
            annual_distribution_rate=Decimal("24.00"),
            distribution_cadence=AssetAccount.InterestCadence.YEARLY,
        )

        projection = build_projection(household)

        # Distributing half: 5000 x 24% = 1200 gross for the year, well above
        # the 3.2% notional Vorabpauschale base for the whole account, so it
        # nets to zero -- summed across both holdings, same as a single
        # account-level rate would.
        self.assertFalse(any(line.section == "Vorabpauschale" for line in projection[12].audit_lines))
        self.assertEqual(projection[12].expenses, Decimal("0.00"))

    def test_capital_allowance_offsets_depot_distribution_tax_until_used(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("150.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("12000.00"), depot_annual_return_rate=Decimal("0.00"),
            depot_annual_distribution_rate=Decimal("12.00"),
            depot_teilfreistellung_rate=Decimal("0.00"),
            depot_distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].depot_income, Decimal("120.00"))
        self.assertEqual(projection[1].depot_income, Decimal("97.50"))
        first_line = next(line for line in projection[0].audit_lines if line.section == "Depot distribution")
        second_line = next(line for line in projection[1].audit_lines if line.section == "Depot distribution")
        self.assertEqual(first_line.note, "120.00 gross, 0.00% partial exemption, 120.00 allowance, 0.00 tax at 25.00% on 12.00% yield")
        self.assertEqual(second_line.note, "120.00 gross, 0.00% partial exemption, 30.00 allowance, 22.50 tax at 25.00% on 12.00% yield")

    def test_capital_allowance_is_shared_across_capital_income_sources(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("200.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"),
            savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.MONTHLY,
            savings_interest_tax_rate=Decimal("25.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("12000.00"),
            depot_annual_return_rate=Decimal("0.00"),
            depot_annual_distribution_rate=Decimal("12.00"),
            depot_teilfreistellung_rate=Decimal("0.00"),
            depot_distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            name="Family loan",
            borrower="Sibling",
            current_principal=Decimal("12000.00"),
            annual_interest_rate=Decimal("12.00"),
            interest_tax_rate=Decimal("25.00"),
            monthly_principal_repayment=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            is_active=True,
        )

        projection = build_projection(household)
        month = projection[0]
        lines = {(line.section, line.name): line for line in month.audit_lines}

        # Contributor order consumes one household allowance bucket for the year:
        # private loan uses 120, savings uses the remaining 80, depot gets none.
        self.assertEqual(month.investment_income, Decimal("120.00"))
        self.assertEqual(month.savings_interest_income, Decimal("110.00"))
        self.assertEqual(month.depot_income, Decimal("90.00"))
        self.assertEqual(month.income, Decimal("320.00"))
        self.assertIn("120.00 gross, 120.00 allowance, 0.00 tax", lines[("Private loan interest", "Family loan")].note)
        self.assertEqual(lines[("Savings interest", "Tagesgeld")].note, "120.00 gross, 80.00 allowance, 10.00 tax at 25.00%")
        self.assertEqual(
            lines[("Depot distribution", "Depot")].note,
            "120.00 gross, 0.00% partial exemption, 0.00 allowance, 30.00 tax at 25.00% on 12.00% yield",
        )

    def test_depot_distribution_uses_teilfreistellung_before_tax(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Equity ETF depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("12000.00"),
            depot_annual_return_rate=Decimal("0.00"),
            depot_annual_distribution_rate=Decimal("12.00"),
            depot_teilfreistellung_rate=Decimal("30.00"),
            depot_distribution_cadence=AssetAccount.InterestCadence.MONTHLY,
        )

        projection = build_projection(household)
        line = next(line for line in projection[0].audit_lines if line.section == "Depot distribution")

        # 120 gross x 70% taxable x 25% tax = 21 tax, so 99 net.
        self.assertEqual(projection[0].depot_income, Decimal("99.00"))
        self.assertEqual(line.note, "120.00 gross, 30.00% partial exemption, 0.00 allowance, 21.00 tax at 25.00% on 12.00% yield")

    def test_accumulating_depot_vorabpauschale_hits_cash_in_following_january(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=13,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            vorabpauschale_basiszins_rate=Decimal("3.20"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        cash = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        household.default_operating_account = cash
        household.save()
        AssetAccount.objects.create(
            household=household,
            name="Accumulating ETF depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_annual_return_rate=Decimal("10.00"),
            depot_teilfreistellung_rate=Decimal("30.00"),
            depot_vorabpauschale_enabled=True,
        )

        projection = build_projection(household)
        january_2027 = projection[12]
        line = next(line for line in january_2027.audit_lines if line.section == "Vorabpauschale")

        # 10000 x 3.20% x 70% = 224 notional base; 30% Teilfreistellung leaves
        # 156.80 taxable at 25%, so 39.20 cash tax in January 2027.
        self.assertEqual(january_2027.expenses, Decimal("39.20"))
        self.assertEqual(january_2027.liquid_balance, Decimal("960.80"))
        self.assertEqual(line.cash_effect, Decimal("-39.20"))
        self.assertIn("2026 notional base 224.00", line.note)

    def test_depot_distributions_reduce_vorabpauschale_to_zero(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=13,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            vorabpauschale_basiszins_rate=Decimal("3.20"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Distributing ETF depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_annual_return_rate=Decimal("0.00"),
            depot_annual_distribution_rate=Decimal("12.00"),
            depot_teilfreistellung_rate=Decimal("30.00"),
            depot_distribution_cadence=AssetAccount.InterestCadence.YEARLY,
            depot_vorabpauschale_enabled=True,
        )

        projection = build_projection(household)

        self.assertFalse(any(line.section == "Vorabpauschale" for line in projection[12].audit_lines))
        self.assertEqual(projection[12].expenses, Decimal("0.00"))

    def test_taxable_income_rule_is_netted_by_household_rate(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            income_tax_rate=Decimal("40.00"),
        )
        MoneyRule.objects.create(
            household=household, name="Salary", kind=MoneyRule.Kind.INCOME,
            amount=Decimal("5000.00"), is_taxable=True,
        )
        MoneyRule.objects.create(
            household=household, name="Net side gig", kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"), is_taxable=False,
        )

        projection = build_projection(household)

        # 5000 gross at 40% -> 3000 net; the untaxed 1000 stays. Total 4000.
        self.assertEqual(projection[0].income, Decimal("4000.00"))
        self.assertEqual(projection[0].income_rule_income, Decimal("4000.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("4000.00"))

    def test_cash_goal_funded_by_depot_draw_when_enabled(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            fund_cash_goal_from_depot=True,
            capital_gains_tax_rate=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"), depot_annual_return_rate=Decimal("0.00"),
        )
        MoneyRule.objects.create(
            household=household, name="Pension", kind=MoneyRule.Kind.INCOME, amount=Decimal("600.00")
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("12000.00"), start_year=2026)

        month = build_projection(household)[0]

        # Monthly goal 1000 is spent; 600 income leaves a 400 shortfall drawn from depot.
        self.assertEqual(month.expenses, Decimal("1000.00"))
        self.assertEqual(month.depot_draw, Decimal("400.00"))
        self.assertEqual(month.liquid_balance, Decimal("0.00"))
        self.assertEqual(month.invested_balance, Decimal("49600.00"))
        self.assertEqual(month.income, Decimal("600.00"))  # the draw is not income
        sections = {line.section for line in month.audit_lines}
        self.assertIn("Cash goal spending", sections)
        self.assertIn("Depot draw", sections)

    def test_depot_draw_covers_negative_liquid_without_cash_goal(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            fund_cash_goal_from_depot=True,
            capital_gains_tax_rate=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"), depot_annual_return_rate=Decimal("0.00"),
        )
        MoneyRule.objects.create(
            household=household, name="Big bill", kind=MoneyRule.Kind.EXPENSE, amount=Decimal("2000.00")
        )

        month = build_projection(household)[0]

        self.assertEqual(month.depot_draw, Decimal("2000.00"))
        self.assertEqual(month.liquid_balance, Decimal("0.00"))
        self.assertEqual(month.invested_balance, Decimal("48000.00"))

    def test_depot_draw_uses_capital_income_allowance_before_grossing_up_tax(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            fund_cash_goal_from_depot=True,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("1000.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"), depot_annual_return_rate=Decimal("0.00"),
            depot_teilfreistellung_rate=Decimal("0.00"),
        )
        MoneyRule.objects.create(
            household=household, name="Spending", kind=MoneyRule.Kind.EXPENSE, amount=Decimal("2000.00")
        )

        month = build_projection(household)[0]
        line = next(line for line in month.audit_lines if line.section == "Depot draw")

        self.assertEqual(month.depot_draw, Decimal("2000.00"))
        self.assertEqual(month.invested_balance, Decimal("7666.67"))
        self.assertEqual(line.note, "Sold 2333.33 depot, 0.00% partial exemption, 1000.00 allowance, 333.33 capital-gains tax at 25.00%")

    def test_cash_goal_depot_draw_uses_remaining_capital_allowance_later_in_year(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            fund_cash_goal_from_depot=True,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("1000.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"),
            savings_annual_interest_rate=Decimal("1.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.YEARLY,
            savings_interest_tax_rate=Decimal("25.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"),
            depot_annual_return_rate=Decimal("0.00"),
            depot_teilfreistellung_rate=Decimal("0.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        MoneyRule.objects.create(
            household=household,
            account=giro,
            name="Pension",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1200.00"),
            start_month=date(2026, 1, 1),
        )
        TrueExpense.objects.create(
            household=household,
            account=giro,
            name="Annual health buffer",
            amount=Decimal("2000.00"),
            cadence=TrueExpense.Cadence.ONCE,
            first_due_month=date(2026, 12, 1),
        )
        CashGoal.objects.create(
            household=household,
            name="Need",
            annual_amount=Decimal("14400.00"),
            start_year=2026,
        )
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="Move savings to depot",
            asset_type=PlannedInvestmentPurchase.AssetType.ETF,
            source_account=AssetAccount.objects.get(household=household, name="Tagesgeld"),
            target_account=AssetAccount.objects.get(household=household, name="Depot"),
            purchase_amount=Decimal("12120.00"),
            purchase_month=date(2026, 12, 1),
        )

        projection = build_projection(household)
        yearly = build_yearly_projection(projection, household.cash_goals.all())
        january = projection[0]
        december = projection[11]
        draw_line = next(line for line in december.audit_lines if line.section == "Depot draw")
        result = check_projection_integrity(projection, yearly, household.accounts.all())

        self.assertEqual(january.depot_draw, Decimal("0.00"))
        self.assertEqual(january.invested_balance, Decimal("50000.00"))
        self.assertEqual(december.savings_interest_income, Decimal("120.00"))
        self.assertEqual(december.depot_draw, Decimal("2000.00"))
        self.assertEqual(december.liquid_balance, Decimal("0.00"))
        self.assertEqual(december.invested_balance, Decimal("59746.67"))
        self.assertEqual(draw_line.note, "Sold 2373.33 depot, 0.00% partial exemption, 880.00 allowance, 373.33 capital-gains tax at 25.00%")
        self.assertEqual(yearly[0].annual_cash_goal, Decimal("14400.00"))
        self.assertEqual(yearly[0].cash_goal_gap, Decimal("0.00"))
        self.assertEqual(yearly[0].depot_draw, Decimal("2000.00"))
        self.assertTrue(result["ok"], result["failures"])

    def test_depot_draw_uses_teilfreistellung_before_grossing_up_tax(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            fund_cash_goal_from_depot=True,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Equity ETF depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_annual_return_rate=Decimal("0.00"),
            depot_teilfreistellung_rate=Decimal("30.00"),
        )
        MoneyRule.objects.create(
            household=household, name="Spending", kind=MoneyRule.Kind.EXPENSE, amount=Decimal("2000.00")
        )

        month = build_projection(household)[0]
        line = next(line for line in month.audit_lines if line.section == "Depot draw")

        self.assertEqual(month.depot_draw, Decimal("2000.00"))
        self.assertEqual(month.invested_balance, Decimal("7575.76"))
        self.assertEqual(line.note, "Sold 2424.24 depot, 30.00% partial exemption, 0.00 allowance, 424.24 capital-gains tax at 25.00%")

    def test_depot_draw_off_by_default(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT, balance=Decimal("50000.00")
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("12000.00"), start_year=2026)

        month = build_projection(household)[0]

        self.assertEqual(month.depot_draw, Decimal("0.00"))
        self.assertEqual(month.expenses, Decimal("0.00"))

    def test_income_components_sum_to_total_income(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_years=1,
        )
        MoneyRule.objects.create(
            household=household, name="Salary", kind=MoneyRule.Kind.INCOME, amount=Decimal("3000.00")
        )
        AssetAccount.objects.create(
            household=household, name="Tagesgeld", account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"), savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.MONTHLY, savings_interest_tax_rate=Decimal("25.00"),
        )

        year = build_yearly_projection(build_projection(household), household.cash_goals.all())[0]
        components = (
            year.investment_income + year.savings_interest_income + year.retirement_income
            + year.equity_income + year.salary_change_income + year.child_income
            + year.scenario_income + year.income_rule_income + year.depot_income
        )
        self.assertEqual(components, year.income)
        self.assertEqual(year.income_rule_income, Decimal("36000.00"))

        analytics = build_analytics_data([], [year], household)
        self.assertEqual(analytics["yearly"][0]["incomeRuleIncome"], "36000.00")

    def test_income_timeline_page_renders(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=Household.objects.first(), name="Salary", kind=MoneyRule.Kind.INCOME, amount=Decimal("3000.00")
        )
        response = self.client.get(reverse("planner:income_timeline"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Income timeline")
        self.assertContains(response, "Major changes")

    def test_income_timeline_source_filter_shows_only_matching_column(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            capital_gains_tax_rate=Decimal("25.00"),
        )
        MoneyRule.objects.create(
            household=household, name="Salary", kind=MoneyRule.Kind.INCOME, amount=Decimal("3000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Distributing ETF",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("1000.00"),
            annual_distribution_rate=Decimal("2.00"),
        )

        unfiltered = self.client.get(reverse("planner:income_timeline"))
        self.assertContains(unfiltered, "Income rule")
        self.assertContains(unfiltered, "Depot distribution")
        self.assertNotContains(unfiltered, "Clear filter")

        filtered = self.client.get(reverse("planner:income_timeline"), {"source": "Depot distribution"})
        self.assertContains(filtered, "Depot distribution")
        self.assertNotContains(filtered, "Income rule")
        self.assertContains(filtered, "Clear filter")

        invalid_filter = self.client.get(reverse("planner:income_timeline"), {"source": "Not a real source"})
        self.assertContains(invalid_filter, "Income rule")
        self.assertContains(invalid_filter, "Depot distribution")
        self.assertNotContains(invalid_filter, "Clear filter")

    def test_projection_contributor_runs_in_isolation(self):
        # The refactored engine lets a single financial concept be exercised on
        # its own against a ProjectionContext, without the whole month loop.
        from planner.projections import (
            DepotGrowthContributor,
            MonthState,
            ProjectionContext,
            monthly_rate_from_annual_percent,
        )

        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
            depot_annual_return_rate=Decimal("12.00"),
        )
        context = ProjectionContext(
            projection_start=date(2026, 1, 1),
            liquid_balance=Decimal("0.00"),
            invested_balance=Decimal("1000.00"),
            other_asset_balance=Decimal("0.00"),
            liability_balance=Decimal("0.00"),
            debt_balances={},
            cash_balances={},
            savings_balances={},
            depot_balances={depot.id: Decimal("1000.00")},
            depot_year_opening_balances={},
            depot_gross_distributions_by_year={},
            private_loan_balances={},
            real_estate_balances={},
            debt_by_account_id={},
            depot_growth_rates={depot.id: monthly_rate_from_annual_percent(Decimal("12.00"))},
            depot_monthly_return_rates=[],
            default_operating_account=None,
            default_income_growth_rate=Decimal("0.00"),
            retirement_deduction_rate=Decimal("0.00"),
            capital_tax_rate=Decimal("0.00"),
            income_tax_rate=Decimal("0.00"),
            capital_income_allowance=Decimal("0.00"),
            capital_allowance_used={},
            vorabpauschale_basiszins_rate=Decimal("3.20"),
        )
        state = MonthState(index=0, month=date(2026, 1, 1))

        DepotGrowthContributor([depot]).apply(context, state)

        self.assertEqual(state.depot_growth, Decimal("9.49"))
        self.assertEqual(context.invested_balance, Decimal("1009.49"))
        self.assertEqual(context.depot_balances[depot.id], Decimal("1009.49"))

    def test_snapshot_compare_page_shows_deltas(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
            currency="EUR",
        )
        snapshot = Snapshot.objects.create(
            household=household,
            name="Before import",
            snapshot_date=date(2026, 6, 25),
            summary=build_snapshot_summary(household),
        )
        account.balance = Decimal("900.00")
        account.save()
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.get(reverse("planner:snapshot_compare", args=[snapshot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Snapshot Comparison")
        self.assertContains(response, "Before import")
        self.assertContains(response, "400.00 EUR")
        self.assertContains(response, "changed")

    def test_projection_change_page_compares_latest_snapshot_forecast(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=2,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"),
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        snapshot = Snapshot.objects.create(
            household=household,
            name="Before raise",
            snapshot_date=date(2026, 1, 1),
            summary=build_snapshot_summary(household),
        )
        account.balance = Decimal("1500.00")
        account.save()
        MoneyRule.objects.create(
            household=household,
            name="Raise",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("500.00"),
            start_month=date(2027, 1, 1),
        )
        current_summary = build_snapshot_summary(household)

        comparison = compare_projection_summaries(snapshot.summary, current_summary)
        drivers = build_projection_change_drivers(snapshot.summary, current_summary)

        year_2027 = next(row for row in comparison["rows"] if row["year"] == 2027)
        income_delta = next(field for field in year_2027["fields"] if field["key"] == "income")
        self.assertEqual(year_2027["status"], "changed")
        self.assertEqual(income_delta["delta"], "6000.00")
        driver_labels = [
            row["label"]
            for group in drivers["groups"]
            for row in group["rows"]
        ]
        self.assertIn("Income and expense rules: Raise", driver_labels)
        self.assertIn("Accounts", driver_labels)

        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )
        response = self.client.get(reverse("planner:snapshot_projection_changes"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Projection changed since last snapshot")
        self.assertContains(response, "Before raise")
        self.assertContains(response, "6000.00 EUR")
        self.assertContains(response, "Changed years")
        self.assertContains(response, "Likely change drivers")
        self.assertContains(response, "Income and expense rules: Raise")
        self.assertContains(response, "Accounts")

    def test_snapshot_baseline_type_and_pin_are_managed(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        first = Snapshot.objects.create(
            household=household,
            name="Day zero",
            snapshot_type=Snapshot.SnapshotType.BASELINE,
            is_baseline=True,
            snapshot_date=date(2026, 1, 1),
            summary=build_snapshot_summary(household),
        )
        second = Snapshot.objects.create(
            household=household,
            name="Reset baseline",
            snapshot_type=Snapshot.SnapshotType.ANNUAL,
            is_baseline=True,
            snapshot_date=date(2026, 2, 1),
            summary=build_snapshot_summary(household),
        )

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertFalse(first.is_baseline)
        self.assertTrue(second.is_baseline)

    def test_snapshot_form_keeps_is_baseline_and_type_consistent(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.post(reverse("planner:snapshots"), {
            "name": "Pinned",
            "snapshot_type": Snapshot.SnapshotType.MANUAL,
            "is_baseline": "on",
            "snapshot_date": "2026-01-01",
            "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        pinned = Snapshot.objects.get(name="Pinned")
        self.assertEqual(pinned.snapshot_type, Snapshot.SnapshotType.BASELINE)
        self.assertTrue(pinned.is_baseline)

        response = self.client.post(reverse("planner:snapshots"), {
            "name": "Typed baseline",
            "snapshot_type": Snapshot.SnapshotType.BASELINE,
            "snapshot_date": "2026-02-01",
            "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        typed = Snapshot.objects.get(name="Typed baseline")
        self.assertTrue(typed.is_baseline)

        response = self.client.post(reverse("planner:snapshots"), {
            "name": "Annual",
            "snapshot_type": Snapshot.SnapshotType.ANNUAL,
            "snapshot_date": "2026-03-01",
            "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        annual = Snapshot.objects.get(name="Annual")
        self.assertFalse(annual.is_baseline)
        self.assertEqual(annual.snapshot_type, Snapshot.SnapshotType.ANNUAL)

    def test_backfill_baseline_snapshot_migration_flags_earliest_snapshot(self):
        import importlib

        from django.apps import apps as global_apps

        household = Household.objects.create(
            name="Legacy household",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        older = Snapshot.objects.create(
            household=household,
            name="Older",
            snapshot_type=Snapshot.SnapshotType.MANUAL,
            snapshot_date=date(2026, 1, 1),
            summary={},
        )
        Snapshot.objects.create(
            household=household,
            name="Newer",
            snapshot_type=Snapshot.SnapshotType.MANUAL,
            snapshot_date=date(2026, 2, 1),
            summary={},
        )
        already_pinned_household = Household.objects.create(
            name="Already pinned",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        pinned = Snapshot.objects.create(
            household=already_pinned_household,
            name="Existing baseline",
            snapshot_type=Snapshot.SnapshotType.BASELINE,
            is_baseline=True,
            snapshot_date=date(2026, 3, 1),
            summary={},
        )

        migration_module = importlib.import_module("planner.migrations.0064_backfill_baseline_snapshot")
        migration_module.backfill_baseline_snapshot(global_apps, None)

        older.refresh_from_db()
        self.assertTrue(older.is_baseline)
        self.assertEqual(older.snapshot_type, Snapshot.SnapshotType.BASELINE)
        self.assertEqual(household.snapshots.filter(is_baseline=True).count(), 1)

        pinned.refresh_from_db()
        self.assertTrue(pinned.is_baseline)
        self.assertEqual(already_pinned_household.snapshots.filter(is_baseline=True).count(), 1)

    def test_backfill_holding_distribution_rate_migration_copies_account_rate(self):
        import importlib

        from django.apps import apps as global_apps

        household = Household.objects.create(
            name="Legacy holdings household",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
            depot_annual_distribution_rate=Decimal("1.20"),
            depot_distribution_cadence=AssetAccount.InterestCadence.QUARTERLY,
        )
        first = DepotHolding.objects.create(
            asset_account=depot, name="Fund A", quantity=Decimal("10.000000"), latest_price=Decimal("500.00"),
        )
        second = DepotHolding.objects.create(
            asset_account=depot, name="Fund B", quantity=Decimal("10.000000"), latest_price=Decimal("500.00"),
        )
        # Account-balance depots have no holdings to backfill onto; the migration
        # must leave holdings under such accounts (if any exist) untouched.
        flat_depot = AssetAccount.objects.create(
            household=household,
            name="Flat depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("5000.00"),
            depot_annual_distribution_rate=Decimal("2.00"),
        )

        migration_module = importlib.import_module("planner.migrations.0066_backfill_holding_distribution_rate")
        migration_module.backfill_holding_distribution_rate(global_apps, None)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.annual_distribution_rate, Decimal("1.20"))
        self.assertEqual(first.distribution_cadence, AssetAccount.InterestCadence.QUARTERLY)
        self.assertEqual(second.annual_distribution_rate, Decimal("1.20"))
        self.assertEqual(flat_depot.holdings.count(), 0)

    def test_snapshots_page_defaults_first_snapshot_to_baseline(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.get(reverse("planner:snapshots"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "snapshot_type")
        self.assertContains(response, "is_baseline")
        self.assertContains(response, "Baseline")

    def test_baseline_snapshot_compare_shortcuts_render(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        baseline = Snapshot.objects.create(
            household=household,
            name="Day zero",
            snapshot_type=Snapshot.SnapshotType.BASELINE,
            is_baseline=True,
            snapshot_date=date(2026, 1, 1),
            summary=build_snapshot_summary(household),
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )
        compare_url = reverse("planner:snapshot_compare", args=[baseline.pk])

        for url_name in ["dashboard", "snapshots", "real_data_start"]:
            response = self.client.get(reverse(f"planner:{url_name}"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Compare current to baseline")
            self.assertContains(response, compare_url)

    def test_snapshot_review_needs_two_snapshots(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        Snapshot.objects.create(
            household=household,
            name="Only snapshot",
            snapshot_date=date(2026, 6, 25),
            summary={"household": {"currency": "EUR"}, "totals": {"net_worth": "500.00"}},
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.get(reverse("planner:snapshot_review"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Two snapshots needed")

    def test_snapshot_review_defaults_to_oldest_and_newest_snapshots(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        old = Snapshot.objects.create(
            household=household,
            name="Start 2026",
            snapshot_date=date(2026, 1, 1),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "500.00", "net_worth": "500.00"}},
        )
        new = Snapshot.objects.create(
            household=household,
            name="End 2026",
            snapshot_date=date(2026, 12, 31),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "900.00", "net_worth": "900.00"}},
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.get(reverse("planner:snapshot_review"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["baseline"], old)
        self.assertEqual(response.context["comparison"], new)
        self.assertContains(response, "Annual Review")
        self.assertContains(response, "400.00")

    def test_snapshot_review_allows_explicit_snapshot_pair(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        first = Snapshot.objects.create(
            household=household,
            name="First",
            snapshot_date=date(2026, 1, 1),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "500.00", "net_worth": "500.00"}},
        )
        second = Snapshot.objects.create(
            household=household,
            name="Second",
            snapshot_date=date(2026, 6, 1),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "700.00", "net_worth": "700.00"}},
        )
        third = Snapshot.objects.create(
            household=household,
            name="Third",
            snapshot_date=date(2026, 12, 31),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "1200.00", "net_worth": "1200.00"}},
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.get(
            reverse("planner:snapshot_review"),
            {"baseline": first.pk, "comparison": second.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["baseline"], first)
        self.assertEqual(response.context["comparison"], second)
        self.assertContains(response, "200.00")
        self.assertNotEqual(response.context["comparison"], third)

    def test_snapshot_review_falls_back_when_pair_ids_are_invalid(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        old = Snapshot.objects.create(
            household=household,
            name="Start",
            snapshot_date=date(2026, 1, 1),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "500.00", "net_worth": "500.00"}},
        )
        new = Snapshot.objects.create(
            household=household,
            name="End",
            snapshot_date=date(2026, 12, 31),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "900.00", "net_worth": "900.00"}},
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.get(
            reverse("planner:snapshot_review"),
            {"baseline": "9999", "comparison": "8888"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["baseline"], old)
        self.assertEqual(response.context["comparison"], new)

    def test_snapshot_review_saves_and_updates_review_notes(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        baseline = Snapshot.objects.create(
            household=household,
            name="Start",
            snapshot_date=date(2026, 1, 1),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "500.00", "net_worth": "500.00"}},
        )
        comparison = Snapshot.objects.create(
            household=household,
            name="End",
            snapshot_date=date(2026, 12, 31),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "900.00", "net_worth": "900.00"}},
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.post(
            reverse("planner:snapshot_review"),
            {
                "baseline": baseline.pk,
                "comparison": comparison.pk,
                "title": "2026 review",
                "review_date": "2027-01-05",
                "planned_summary": "Keep savings plan active.",
                "actual_summary": "Savings grew.",
                "lessons_learned": "Monthly review helps.",
                "next_actions": "Rebalance cash.",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('planner:snapshot_review')}?baseline={baseline.pk}&comparison={comparison.pk}",
        )
        saved_review = SnapshotReview.objects.get()
        self.assertEqual(saved_review.title, "2026 review")
        self.assertEqual(saved_review.baseline_snapshot, baseline)
        self.assertEqual(saved_review.comparison_snapshot, comparison)
        self.assertEqual(saved_review.next_actions, "Rebalance cash.")

        response = self.client.post(
            reverse("planner:snapshot_review"),
            {
                "baseline": baseline.pk,
                "comparison": comparison.pk,
                "title": "2026 review updated",
                "review_date": "2027-01-06",
                "planned_summary": "Keep savings plan active.",
                "actual_summary": "Savings grew more than expected.",
                "lessons_learned": "Monthly review helps.",
                "next_actions": "Increase ETF contribution.",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('planner:snapshot_review')}?baseline={baseline.pk}&comparison={comparison.pk}",
        )
        self.assertEqual(SnapshotReview.objects.count(), 1)
        saved_review.refresh_from_db()
        self.assertEqual(saved_review.title, "2026 review updated")
        self.assertEqual(saved_review.next_actions, "Increase ETF contribution.")

    def test_snapshot_review_adds_and_updates_review_actions(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        owner = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        baseline = Snapshot.objects.create(
            household=household,
            name="Start",
            snapshot_date=date(2026, 1, 1),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "500.00", "net_worth": "500.00"}},
        )
        comparison = Snapshot.objects.create(
            household=household,
            name="End",
            snapshot_date=date(2026, 12, 31),
            summary={"household": {"currency": "EUR"}, "totals": {"liquid": "900.00", "net_worth": "900.00"}},
        )
        review = SnapshotReview.objects.create(
            household=household,
            baseline_snapshot=baseline,
            comparison_snapshot=comparison,
            title="2026 review",
            review_date=date(2027, 1, 5),
        )
        FeatureFlag.objects.update_or_create(
            key="snapshots",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["snapshots"]["description"]},
        )

        response = self.client.post(
            reverse("planner:snapshot_review"),
            {
                "action": "add_review_action",
                "baseline": baseline.pk,
                "comparison": comparison.pk,
                "title": "Review mortgage refinance",
                "owner": owner.pk,
                "due_date": "2027-03-31",
                "status": SnapshotReviewAction.Status.OPEN,
                "notes": "Check fixed interest period.",
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('planner:snapshot_review')}?baseline={baseline.pk}&comparison={comparison.pk}",
        )
        review_action = SnapshotReviewAction.objects.get()
        self.assertEqual(review_action.review, review)
        self.assertEqual(review_action.owner, owner)
        self.assertEqual(review_action.status, SnapshotReviewAction.Status.OPEN)

        response = self.client.post(
            reverse("planner:snapshot_review"),
            {
                "action": "update_review_action",
                "baseline": baseline.pk,
                "comparison": comparison.pk,
                "review_action_id": review_action.pk,
                "status": SnapshotReviewAction.Status.DONE,
            },
        )

        self.assertRedirects(
            response,
            f"{reverse('planner:snapshot_review')}?baseline={baseline.pk}&comparison={comparison.pk}",
        )
        review_action.refresh_from_db()
        self.assertEqual(review_action.status, SnapshotReviewAction.Status.DONE)

    def test_import_center_shows_moneymoney_adapter_hint_when_enabled(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        response = self.client.get(reverse("planner:import_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preview from the local MoneyMoney app")
        self.assertContains(response, "Preview MoneyMoney accounts")

    def test_import_center_shows_old_and_new_values_for_updates(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            currency="EUR",
            institution="Old Bank",
            as_of_date=date(2026, 1, 1),
        )
        csv_file = SimpleUploadedFile(
            "accounts.csv",
            b"name,account_type,balance,currency,institution,as_of_date\nGiro,cash,1500.00,EUR,New Bank,2026-02-01\n",
            content_type="text/csv",
        )

        response = self.client.post(reverse("planner:import_center"), {"import_kind": "accounts", "csv_file": csv_file})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Old balance")
        self.assertContains(response, "1000.00 EUR")
        self.assertContains(response, "1500.00")
        self.assertContains(response, "balance, institution, as_of_date")
        batch = household.import_batches.get()
        self.assertEqual(batch.summary["rows"][0]["existing_values"]["balance"], "1000.00")
        self.assertIn("balance", batch.summary["rows"][0]["changes"])

    def test_import_runbook_renders_live_checklist(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:import_runbook"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import runbook")
        self.assertContains(response, "Create household foundation")
        self.assertContains(response, "No account import batch yet")
        self.assertContains(response, "Open setup")

    def test_import_runbook_uses_session_diagnostics(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        session = self.client.session
        session["moneymoney_diagnostics"] = {
            "py_money_installed": True,
            "reachable": True,
            "account_count": 2,
            "portfolio_count": 1,
            "position_count": 4,
            "error": "",
        }
        session.save()

        response = self.client.get(reverse("planner:import_runbook"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Last Diagnostics")
        self.assertContains(response, "reachable")
        self.assertContains(response, "Installed")

    def test_build_import_runbook_tracks_batches_and_next_action(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
            is_active=True,
        )
        CashGoal.objects.create(household=household, name="FIRE", annual_amount=Decimal("36000.00"), start_year=2026)
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            currency="EUR",
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "1000.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                }
            ],
            start_row_number=1,
        )
        summary = dry_run_summary(result)
        summary["import_kind"] = "moneymoney_accounts"
        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.MONEYMONEY,
            status=ImportBatch.Status.APPLIED,
            filename="moneymoney_accounts",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=summary,
        )

        runbook = build_import_runbook(
            household,
            {
                "py_money_installed": True,
                "reachable": True,
                "account_count": 1,
                "portfolio_count": 0,
                "position_count": 0,
                "error": "",
            },
        )

        by_key = {item["key"]: item for item in runbook["items"]}
        self.assertTrue(by_key["setup"]["complete"])
        self.assertTrue(by_key["diagnostics"]["complete"])
        self.assertTrue(by_key["accounts"]["complete"])
        self.assertFalse(by_key["holdings"]["complete"])
        self.assertEqual(runbook["next_item"]["key"], "money_flag")

    def test_moneymoney_accounts_preview_creates_dry_run_batch(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        class FakeConnector:
            def account_rows(self, account_type_overrides=None):
                return [
                    ImportedAccountRow(
                        name="Giro",
                        account_type=AssetAccount.AccountType.CASH,
                        balance="1234.56",
                        currency="EUR",
                        as_of_date="2026-06-25",
                    )
                ]

        with patch("planner.views.MoneyMoneyConnector", return_value=FakeConnector()):
            response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_accounts"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MoneyMoney accounts dry-run completed")
        self.assertContains(response, "Giro")
        self.assertEqual(household.accounts.count(), 0)
        batch = ImportBatch.objects.get()
        self.assertEqual(batch.source, ImportBatch.Source.MONEYMONEY)
        self.assertEqual(batch.summary["import_kind"], "moneymoney_accounts")
        self.assertEqual(batch.valid_count, 1)

    def test_moneymoney_discovery_creates_selection_mappings_without_batch(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        class FakeConnector:
            def account_rows(self, account_type_overrides=None):
                return [
                    ImportedAccountRow(
                        name="Giro",
                        account_type=AssetAccount.AccountType.CASH,
                        balance="1234.56",
                        currency="EUR",
                        source_key="account:one",
                        source_kind="account",
                        as_of_date="2026-06-25",
                    ),
                    ImportedAccountRow(
                        name="Giro",
                        account_type=AssetAccount.AccountType.CASH,
                        balance="200.00",
                        currency="EUR",
                        source_key="account:two",
                        source_kind="account",
                        as_of_date="2026-06-25",
                    ),
                ]

        with patch("planner.views.MoneyMoneyConnector", return_value=FakeConnector()):
            response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_discover_accounts"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MoneyMoney account discovery completed")
        self.assertEqual(ImportBatch.objects.count(), 0)
        self.assertEqual(household.moneymoney_account_mappings.count(), 2)
        self.assertEqual(
            list(household.moneymoney_account_mappings.order_by("source_key").values_list("source_key", flat=True)),
            ["account:one", "account:two"],
        )

    def test_moneymoney_accounts_preview_uses_account_type_overrides(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyMoneyAccountMapping.objects.create(
            household=household,
            source_key="legacy-name:Tagesgeld",
            source_kind="legacy",
            account_name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        class FakeConnector:
            def account_rows(self, account_type_overrides=None):
                return [
                    ImportedAccountRow(
                        name="Tagesgeld",
                        account_type=account_type_overrides["Tagesgeld"],
                        balance="5000.00",
                        currency="EUR",
                        as_of_date="2026-06-25",
                    )
                ]

        with patch("planner.views.MoneyMoneyConnector", return_value=FakeConnector()):
            response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_accounts"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tagesgeld")
        self.assertContains(response, "savings")
        batch = ImportBatch.objects.get()
        self.assertEqual(batch.summary["rows"][0]["values"]["account_type"], AssetAccount.AccountType.SAVINGS)

    def test_moneymoney_mapping_review_uses_latest_account_preview(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Tagesgeld",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "5000.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                },
                {
                    "name": "ETF Depot",
                    "account_type": AssetAccount.AccountType.DEPOT,
                    "balance": "50000.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                },
            ],
            start_row_number=1,
        )
        summary = dry_run_summary(result)
        summary["import_kind"] = "moneymoney_accounts"
        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.MONEYMONEY,
            status=ImportBatch.Status.DRY_RUN,
            filename="moneymoney_accounts",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=summary,
        )

        review = build_moneymoney_mapping_review(household)

        by_name = {row["account_name"]: row for row in review["rows"]}
        self.assertTrue(by_name["Tagesgeld"]["needs_review"])
        self.assertFalse(by_name["ETF Depot"]["needs_review"])
        self.assertEqual(review["needs_review_count"], 1)

    def test_moneymoney_mappings_page_saves_and_removes_overrides(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Tagesgeld",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "5000.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                }
            ],
            start_row_number=1,
        )
        summary = dry_run_summary(result)
        summary["import_kind"] = "moneymoney_accounts"
        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.MONEYMONEY,
            status=ImportBatch.Status.DRY_RUN,
            filename="moneymoney_accounts",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=summary,
        )

        response = self.client.get(reverse("planner:moneymoney_mappings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MoneyMoney mappings")
        self.assertContains(response, "Tagesgeld")
        self.assertContains(response, "default cash")

        response = self.client.post(
            reverse("planner:moneymoney_mappings"),
            {
                "action": "save_existing",
                "source_key": ["legacy-name:Tagesgeld"],
                "source_kind": ["legacy"],
                "account_name": ["Tagesgeld"],
                "import_enabled_0": "on",
                "account_type_0": AssetAccount.AccountType.SAVINGS,
                "notes_0": "Savings account",
            },
        )

        self.assertRedirects(response, reverse("planner:moneymoney_mappings"))
        mapping = MoneyMoneyAccountMapping.objects.get(household=household, account_name="Tagesgeld")
        self.assertEqual(mapping.account_type, AssetAccount.AccountType.SAVINGS)

        response = self.client.post(
            reverse("planner:moneymoney_mappings"),
            {
                "action": "save_existing",
                "source_key": ["legacy-name:Tagesgeld"],
                "source_kind": ["legacy"],
                "account_name": ["Tagesgeld"],
                "account_type_0": "",
                "notes_0": "",
            },
        )

        self.assertRedirects(response, reverse("planner:moneymoney_mappings"))
        mapping.refresh_from_db()
        self.assertFalse(mapping.import_enabled)
        self.assertEqual(mapping.account_type, "")

    def test_moneymoney_mappings_page_adds_manual_override(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.post(
            reverse("planner:moneymoney_mappings"),
            {
                "action": "add_mapping",
                "account_name": "Broker Cash",
                "account_type": AssetAccount.AccountType.SAVINGS,
                "import_enabled": "on",
                "notes": "Manual before preview",
            },
        )

        self.assertRedirects(response, reverse("planner:moneymoney_mappings"))
        mapping = MoneyMoneyAccountMapping.objects.get(household=household, account_name="Broker Cash")
        self.assertEqual(mapping.source_key, "legacy-name:Broker Cash")
        self.assertEqual(mapping.account_type, AssetAccount.AccountType.SAVINGS)

    def test_disabled_moneymoney_account_is_excluded_from_preview(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )
        MoneyMoneyAccountMapping.objects.create(
            household=household,
            source_key="account:index:1:Skip Me",
            source_kind="account",
            account_name="Skip Me",
            import_enabled=False,
        )

        class FakeConnector:
            def account_rows(self, account_type_overrides=None):
                return [
                    ImportedAccountRow(
                        name="Keep Me",
                        account_type=AssetAccount.AccountType.CASH,
                        balance="100.00",
                        currency="EUR",
                        source_key="account:index:0:Keep Me",
                        source_kind="account",
                        as_of_date="2026-06-25",
                    ),
                    ImportedAccountRow(
                        name="Skip Me",
                        account_type=AssetAccount.AccountType.CASH,
                        balance="200.00",
                        currency="EUR",
                        source_key="account:index:1:Skip Me",
                        source_kind="account",
                        as_of_date="2026-06-25",
                    ),
                ]

        with patch("planner.views.MoneyMoneyConnector", return_value=FakeConnector()):
            response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_accounts"})

        self.assertEqual(response.status_code, 200)
        batch = ImportBatch.objects.get()
        imported_names = [row["values"]["name"] for row in batch.summary["rows"]]
        self.assertEqual(imported_names, ["Keep Me"])

    def test_moneymoney_discovery_preserves_disabled_selection(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )
        MoneyMoneyAccountMapping.objects.create(
            household=household,
            source_key="account:old-card",
            source_kind="account",
            account_name="Old Card",
            import_enabled=False,
            notes="Keep out of LiF",
        )

        class FakeConnector:
            def account_rows(self, account_type_overrides=None):
                return [
                    ImportedAccountRow(
                        name="Old Card",
                        account_type=AssetAccount.AccountType.CASH,
                        balance="0.00",
                        currency="EUR",
                        source_key="account:old-card",
                        source_kind="account",
                        as_of_date="2026-06-25",
                    )
                ]

        with patch("planner.views.MoneyMoneyConnector", return_value=FakeConnector()):
            response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_discover_accounts"})

        self.assertEqual(response.status_code, 200)
        mapping = MoneyMoneyAccountMapping.objects.get(household=household, source_key="account:old-card")
        self.assertFalse(mapping.import_enabled)
        self.assertEqual(mapping.notes, "Keep out of LiF")

    def test_moneymoney_depot_holdings_preview_creates_dry_run_batch(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            currency="EUR",
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        class FakeConnector:
            def depot_holding_rows(self):
                return [
                    ImportedDepotHoldingRow(
                        account_name="ING Depot",
                        name="Vanguard FTSE All-World",
                        isin="IE00B3RBWM25",
                        ticker="",
                        asset_class="ETF",
                        quantity="10.500000",
                        latest_price="118.42",
                        currency="EUR",
                        as_of_date="2026-06-25",
                    )
                ]

        with patch("planner.views.MoneyMoneyConnector", return_value=FakeConnector()):
            response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_depot_holdings"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MoneyMoney depot holdings dry-run completed")
        self.assertContains(response, "Vanguard FTSE All-World")
        self.assertEqual(DepotHolding.objects.count(), 0)
        batch = ImportBatch.objects.get()
        self.assertEqual(batch.source, ImportBatch.Source.MONEYMONEY)
        self.assertEqual(batch.summary["import_kind"], "moneymoney_depot_holdings")
        self.assertEqual(batch.valid_count, 1)

    def test_moneymoney_diagnostics_reports_counts_without_batch(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        class FakeConnector:
            def diagnostics(self):
                return type(
                    "Diagnostics",
                    (),
                    {
                        "py_money_installed": True,
                        "reachable": True,
                        "account_count": 2,
                        "portfolio_count": 1,
                        "position_count": 4,
                        "error": "",
                    },
                )()

        with patch("planner.moneymoney_service.MoneyMoneyConnector", return_value=FakeConnector()):
            response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_diagnostics"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MoneyMoney diagnostics completed")
        self.assertContains(response, "Installed")
        self.assertContains(response, "4")
        self.assertEqual(ImportBatch.objects.count(), 0)

    def test_moneymoney_preview_reports_connector_errors(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        with patch("planner.views.MoneyMoneyConnector", side_effect=Exception("MoneyMoney is not running")):
            response = self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_accounts"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MoneyMoney import failed: MoneyMoney is not running")
        self.assertEqual(ImportBatch.objects.count(), 0)

    def test_moneymoney_account_batch_can_be_applied(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "1234.56",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                }
            ],
            start_row_number=1,
        )
        summary = dry_run_summary(result)
        summary["import_kind"] = "moneymoney_accounts"
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.MONEYMONEY,
            status=ImportBatch.Status.DRY_RUN,
            filename="moneymoney_accounts",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=summary,
        )

        with patch("planner.imports.call_command"):
            response = self.client.post(reverse("planner:import_batch_apply", args=[batch.pk]))

        self.assertRedirects(response, reverse("planner:import_batch_detail", args=[batch.pk]))
        self.assertTrue(household.accounts.filter(name="Giro").exists())

    def test_moneymoney_duplicate_account_names_apply_by_source_key(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "100.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                    "source_key": "account:one",
                    "source_kind": "account",
                },
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "200.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                    "source_key": "account:two",
                    "source_kind": "account",
                },
            ],
            start_row_number=1,
        )
        summary = dry_run_summary(result)
        summary["import_kind"] = "moneymoney_accounts"
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.MONEYMONEY,
            status=ImportBatch.Status.DRY_RUN,
            filename="moneymoney_accounts",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=summary,
        )

        with patch("planner.imports.call_command"):
            apply_account_import_batch(batch)

        accounts = household.accounts.order_by("moneymoney_account_key")
        self.assertEqual(accounts.count(), 2)
        self.assertEqual([account.name for account in accounts], ["Giro", "Giro"])
        self.assertEqual([account.balance for account in accounts], [Decimal("100.00"), Decimal("200.00")])

    def test_setup_page_renders_first_run_form(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:setup"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "First-run setup")
        self.assertContains(response, "annual_cash_goal")
        self.assertContains(response, "adult_1_monthly_salary")
        self.assertContains(response, "child_1_kindergeld")
        self.assertContains(response, "Household setup")
        self.assertContains(response, "Household settings")
        self.assertContains(response, "Manage households")
        self.assertContains(response, "Clone or switch household")

    def test_setup_page_creates_repeatable_foundation_data(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        payload = {
            "household_name": "Real Home",
            "currency": "eur",
            "start_month": "2026-06-01",
            "planning_years": "40",
            "annual_cash_goal": "36000.00",
            "adult_1_name": "Alex",
            "adult_1_birth_date": "1986-01-01",
            "adult_1_monthly_salary": "3200.00",
            "adult_2_name": "Sam",
            "adult_2_birth_date": "1986-07-01",
            "adult_2_monthly_salary": "2400.00",
            "child_1_name": "Lina",
            "child_1_birth_date": "2016-01-01",
            "child_1_kindergeld": "255.00",
            "child_2_name": "Noah",
            "child_2_birth_date": "2018-01-01",
            "child_2_kindergeld": "255.00",
        }

        response = self.client.post(reverse("planner:setup"), payload)

        self.assertRedirects(response, reverse("planner:setup"))
        household.refresh_from_db()
        self.assertEqual(household.name, "Real Home")
        self.assertEqual(household.currency, "EUR")
        self.assertEqual(household.planning_years, 40)
        self.assertEqual(household.resolved_display_granularity, Household.DisplayGranularity.YEARLY)
        self.assertEqual(household.people.count(), 4)
        self.assertEqual(household.people.filter(role=Person.Role.ADULT).count(), 2)
        self.assertEqual(household.people.filter(role=Person.Role.CHILD).count(), 2)
        self.assertEqual(household.rules.filter(kind=MoneyRule.Kind.INCOME).count(), 4)
        self.assertEqual(household.rules.get(name="Setup salary adult 1").amount, Decimal("3200.00"))
        self.assertEqual(household.rules.get(name="Setup Kindergeld child 1").amount, Decimal("255.00"))
        self.assertEqual(household.cash_goals.get(name="Baseline FIRE cash need").annual_amount, Decimal("36000.00"))
        self.assertTrue(household.cash_goals.get(name="Baseline FIRE cash need").indexed_to_inflation)

        payload["adult_1_name"] = "Alexandra"
        payload["adult_1_monthly_salary"] = "3500.00"
        payload["annual_cash_goal"] = "42000.00"
        self.client.post(reverse("planner:setup"), payload)

        self.assertEqual(household.people.count(), 4)
        self.assertTrue(household.people.filter(name="Alexandra", notes="Setup slot: adult_1").exists())
        self.assertEqual(household.rules.filter(kind=MoneyRule.Kind.INCOME).count(), 4)
        self.assertEqual(household.rules.get(name="Setup salary adult 1").amount, Decimal("3500.00"))
        self.assertEqual(household.cash_goals.count(), 1)
        self.assertEqual(household.cash_goals.get(name="Baseline FIRE cash need").annual_amount, Decimal("42000.00"))

    def test_setup_page_updates_existing_people_without_setup_slots(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        adult_1 = Person.objects.create(household=household, name="Old adult 1", role=Person.Role.ADULT)
        adult_2 = Person.objects.create(household=household, name="Old adult 2", role=Person.Role.ADULT)
        child_1 = Person.objects.create(household=household, name="Old child 1", role=Person.Role.CHILD)
        child_2 = Person.objects.create(household=household, name="Old child 2", role=Person.Role.CHILD)

        payload = {
            "household_name": "Real Home",
            "currency": "EUR",
            "start_month": "2026-06-01",
            "planning_years": "40",
            "annual_cash_goal": "36000.00",
            "adult_1_name": "Alex",
            "adult_1_birth_date": "1986-01-01",
            "adult_1_monthly_salary": "3200.00",
            "adult_2_name": "Sam",
            "adult_2_birth_date": "1986-07-01",
            "adult_2_monthly_salary": "2400.00",
            "child_1_name": "Lina",
            "child_1_birth_date": "2016-01-01",
            "child_1_kindergeld": "255.00",
            "child_2_name": "Noah",
            "child_2_birth_date": "2018-01-01",
            "child_2_kindergeld": "255.00",
        }

        response = self.client.post(reverse("planner:setup"), payload)

        self.assertRedirects(response, reverse("planner:setup"))
        self.assertEqual(household.people.count(), 4)
        adult_1.refresh_from_db()
        adult_2.refresh_from_db()
        child_1.refresh_from_db()
        child_2.refresh_from_db()
        self.assertEqual(adult_1.name, "Alex")
        self.assertEqual(adult_1.notes, "Setup slot: adult_1")
        self.assertEqual(adult_2.name, "Sam")
        self.assertEqual(child_1.name, "Lina")
        self.assertEqual(child_2.name, "Noah")

    def test_deleting_person_deletes_attached_money_rules(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        MoneyRule.objects.create(
            household=household,
            person=person,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3200.00"),
        )

        person.delete()

        self.assertFalse(MoneyRule.objects.filter(name="Salary").exists())

    def test_retirement_plan_accepts_four_decimal_pension_points(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)

        response = self.client.post(
            reverse("planner:retirement_plan_create"),
            {
                "person": person.pk,
                "name": "DRV",
                "current_pension_points": "31.4416",
                "expected_annual_points": "1.000",
                "pension_value_per_point": "40.79",
                "private_monthly_pension": "0.00",
                "retirement_start_month": "2053-01-01",
                "end_month": "",
                "annual_adjustment_rate": "1.50",
                "is_active": "on",
                "notes": "",
            },
        )

        self.assertRedirects(response, reverse("planner:retirement_plan_index"))
        plan = RetirementPlan.objects.get()
        self.assertEqual(plan.current_pension_points, Decimal("31.4416"))

    def test_setup_page_creates_foundation_from_empty_checkout(self):
        payload = {
            "household_name": "Real Home",
            "currency": "EUR",
            "start_month": "2026-06-01",
            "planning_years": "40",
            "annual_cash_goal": "36000.00",
            "adult_1_name": "Alex",
            "adult_1_birth_date": "1986-01-01",
            "adult_1_monthly_salary": "3200.00",
            "adult_2_name": "Sam",
            "adult_2_birth_date": "1986-07-01",
            "adult_2_monthly_salary": "2400.00",
            "child_1_name": "Lina",
            "child_1_birth_date": "2016-01-01",
            "child_1_kindergeld": "255.00",
            "child_2_name": "Noah",
            "child_2_birth_date": "2018-01-01",
            "child_2_kindergeld": "255.00",
        }

        response = self.client.post(reverse("planner:setup"), payload)

        self.assertRedirects(response, reverse("planner:setup"))
        household = Household.objects.get()
        self.assertEqual(household.name, "Real Home")
        self.assertEqual(household.people.count(), 4)
        self.assertEqual(household.rules.filter(kind=MoneyRule.Kind.INCOME).count(), 4)
        self.assertEqual(household.cash_goals.get(name="Baseline FIRE cash need").annual_amount, Decimal("36000.00"))

    def test_quality_report_detects_depot_and_debt_risks(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        # Account-balance valuation: the stated balance is authoritative and is
        # NOT auto-synced, so a holdings mismatch is a real thing to flag.
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
            depot_valuation=AssetAccount.DepotValuation.ACCOUNT_BALANCE,
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="ETF",
            quantity=Decimal("1.000000"),
            latest_price=Decimal("900.00"),
        )
        loan = AssetAccount.objects.create(
            household=household,
            name="Loan",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("10000.00"),
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Depot balance differs from holdings", titles)
        self.assertIn("Loan is a loan without debt details", titles)
        self.assertEqual(report["total"], len(report["issues"]))

    def test_quality_accepts_negative_linked_loan_balance_matching_principal(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        loan = AssetAccount.objects.create(
            household=household,
            name="Loan",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("-10000.00"),
        )
        Debt.objects.create(
            household=household,
            account=loan,
            name="Loan repayment",
            current_principal=Decimal("10000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("500.00"),
        )
        loan.balance = Decimal("-10000.00")
        loan.save(update_fields=["balance"])

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertNotIn("Loan repayment principal differs from loan account", titles)

    def test_quality_report_flags_negative_source_account_forecast(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("500.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        TransferRule.objects.create(
            household=household,
            name="ETF top-up",
            amount=Decimal("1000.00"),
            cadence=TransferRule.Cadence.ONCE,
            source_account=savings,
            target_account=depot,
            start_month=date(2026, 1, 1),
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Projection has negative liquidity", titles)
        self.assertIn("Tagesgeld goes negative in the account forecast", titles)

    def test_quality_report_flags_emergency_fund_target_gap(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
            emergency_fund_months=Decimal("3.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1000.00"),
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Emergency fund target is not met", titles)

    def test_quality_report_flags_unrouted_money_rules_without_default_account(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
            start_month=date(2026, 1, 1),
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Recurring rules are not routed to an account", titles)

        household.default_operating_account = household.accounts.get(name="Giro")
        household.save(update_fields=["default_operating_account"])
        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertNotIn("Recurring rules are not routed to an account", titles)

    def test_quality_report_flags_unrouted_future_cash_flows_without_default_account(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        SalaryChange.objects.create(
            person=adult,
            name="Raise",
            start_month=date(2026, 2, 1),
            monthly_net_income_delta=Decimal("200.00"),
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Future cash-flow items are not routed to an account", titles)

        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertNotIn("Future cash-flow items are not routed to an account", titles)

    def test_quality_report_flags_dangerous_double_count_and_missing_payout_assumptions(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            fund_cash_goal_from_depot=True,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_annual_distribution_rate=Decimal("2.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Living costs",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Manual ETF distributions",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("50.00"),
            category="Investment distributions",
        )
        CashGoal.objects.create(
            household=household,
            name="FIRE spending",
            annual_amount=Decimal("36000.00"),
            start_year=2026,
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Target maturity bond",
            asset_class="Bond target maturity",
            quantity=Decimal("1.000000"),
            latest_price=Decimal("1000.00"),
            payout_date=date(2029, 1, 1),
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            name="Future loan",
            current_principal=Decimal("10000.00"),
            annual_interest_rate=Decimal("4.00"),
            monthly_principal_repayment=Decimal("0.00"),
            disbursement_month=date(2027, 1, 1),
            start_month=date(2027, 2, 1),
            end_month=date(2030, 1, 1),
        )
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="Planned bond",
            asset_type=PlannedInvestmentPurchase.AssetType.BOND,
            target_account=depot,
            purchase_amount=Decimal("1000.00"),
            purchase_month=date(2027, 1, 1),
            payout_date=date(2029, 1, 1),
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Cash goal draw may double-count expenses", titles)
        self.assertIn("Depot distributions may be double-counted", titles)
        self.assertIn("Target maturity bond has no explicit payout amount", titles)
        self.assertIn("Future loan has future disbursement without source account", titles)
        self.assertIn("Planned bond has no explicit payout amount", titles)

    def test_quality_report_flags_double_count_from_holding_level_distribution(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("10000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="Distributing ETF",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("1000.00"),
            annual_distribution_rate=Decimal("2.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Manual ETF distributions",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("50.00"),
            category="Investment distributions",
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Depot distributions may be double-counted", titles)

    @override_settings(BACKUP_DIR="/tmp/lif-missing-test-backups", LIF_REQUIRE_LOGIN=False, DEBUG=True)
    def test_quality_report_detects_operational_and_planning_gaps(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        Person.objects.create(household=household, name="Kid", role=Person.Role.CHILD)
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("5000.00"),
            savings_annual_interest_rate=Decimal("0.00"),
        )
        MoneyRule.objects.create(
            household=household,
            person=adult,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("4000.00"),
        )
        EquityGrant.objects.create(
            household=household,
            person=adult,
            name="RSU",
            gross_vest_value=Decimal("1000.00"),
            withholding_rate=Decimal("120.00"),
            first_vest_month=date(2026, 1, 1),
            last_vest_month=date(2026, 12, 1),
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("No local backup found", titles)
        self.assertIn("Login protection is disabled", titles)
        self.assertIn("Debug mode is enabled", titles)
        self.assertIn("No child benefit income assigned", titles)
        self.assertIn("Tagesgeld has no savings interest rate", titles)
        self.assertIn("Alex has no retirement plan", titles)
        self.assertIn("RSU has invalid withholding", titles)

    def test_retirement_health_checks_detect_projection_risks(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=8,
            pension_tax_rate=Decimal("18.00"),
            capital_gains_tax_rate=Decimal("25.00"),
            health_insurance_rate=Decimal("11.00"),
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        AssetAccount.objects.create(
            household=household,
            name="Checking",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
        )
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Statutory pension",
            current_pension_points=Decimal("5.000"),
            expected_annual_points=Decimal("0.000"),
            pension_value_per_point=Decimal("40.00"),
            retirement_start_month=date(2028, 1, 1),
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("24000.00"), start_year=2028)

        issues = build_retirement_health_issues(household)
        titles = {item.title for item in issues}

        self.assertIn("Retirement tax assumptions still use defaults", titles)
        self.assertIn("Tax-aware draw exceeds 4% in multiple retirement years", titles)

    def test_retirement_health_checks_detect_horizon_and_missing_cash_goals(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=2,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Late pension",
            current_pension_points=Decimal("5.000"),
            retirement_start_month=date(2035, 1, 1),
        )

        horizon_titles = {item.title for item in build_retirement_health_issues(household)}
        self.assertIn("Planning horizon ends before retirement starts", horizon_titles)

        household.planning_years = 12
        household.save()
        missing_goal_titles = {item.title for item in build_retirement_health_issues(household)}
        self.assertIn("Retirement years have no cash goal", missing_goal_titles)

    def test_account_setup_page_renders_guided_sections(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        response = self.client.get(reverse("planner:account_setup"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add real account")
        self.assertContains(response, "Savings Interest")
        self.assertContains(response, "Depot First Holding")
        self.assertContains(response, "Mortgage Or Loan")
        self.assertContains(response, 'data-account-section="savings"')
        self.assertContains(response, 'data-account-section="depot"')
        self.assertContains(response, 'data-account-section="loan"')
        self.assertContains(response, "data-account-hint")

    def test_account_setup_creates_savings_account(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )

        response = self.client.post(
            reverse("planner:account_setup"),
            {
                "account_type": AssetAccount.AccountType.SAVINGS,
                "name": "Tagesgeld",
                "balance": "12000.00",
                "currency": "eur",
                "institution": "Bank",
                "as_of_date": "2026-06-26",
                "savings_annual_interest_rate": "2.50",
                "savings_interest_cadence": AssetAccount.InterestCadence.QUARTERLY,
                "savings_interest_tax_rate": "25.00",
            },
        )

        account = household.accounts.get(name="Tagesgeld")
        self.assertRedirects(response, reverse("planner:account_detail", args=[account.pk]))
        self.assertEqual(account.account_type, AssetAccount.AccountType.SAVINGS)
        self.assertEqual(account.currency, "EUR")
        self.assertEqual(account.savings_annual_interest_rate, Decimal("2.50"))
        self.assertEqual(account.savings_interest_cadence, AssetAccount.InterestCadence.QUARTERLY)

    def test_account_setup_creates_depot_with_first_holding(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )

        response = self.client.post(
            reverse("planner:account_setup"),
            {
                "account_type": AssetAccount.AccountType.DEPOT,
                "name": "ING Depot",
                "balance": "50000.00",
                "currency": "EUR",
                "depot_valuation": AssetAccount.DepotValuation.HOLDINGS_SUM,
                "holding_name": "Vanguard FTSE All-World",
                "holding_isin": "IE00B3RBWM25",
                "holding_ticker": "VGWL",
                "holding_asset_class": "ETF distributing",
                "holding_quantity": "120.500000",
                "holding_latest_price": "118.42",
            },
        )

        account = household.accounts.get(name="ING Depot")
        self.assertRedirects(response, reverse("planner:account_detail", args=[account.pk]))
        self.assertEqual(account.account_type, AssetAccount.AccountType.DEPOT)
        self.assertEqual(account.depot_valuation, AssetAccount.DepotValuation.HOLDINGS_SUM)
        holding = account.holdings.get()
        self.assertEqual(holding.isin, "IE00B3RBWM25")
        self.assertEqual(holding.quantity, Decimal("120.500000"))

    def test_account_setup_creates_mortgage_account_and_debt(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )

        response = self.client.post(
            reverse("planner:account_setup"),
            {
                "account_type": AssetAccount.AccountType.LOAN,
                "name": "Mortgage A",
                "balance": "300000.00",
                "currency": "EUR",
                "debt_name": "Mortgage A repayment",
                "debt_annual_interest_rate": "3.20",
                "debt_monthly_payment": "1450.00",
                "debt_start_month": "2026-01-01",
                "debt_end_month": "2056-01-01",
                "debt_fixed_interest_until": "2036-01-01",
                "debt_refinance_annual_interest_rate": "4.00",
                "debt_refinance_monthly_payment": "1600.00",
            },
        )

        account = household.accounts.get(name="Mortgage A")
        self.assertRedirects(response, reverse("planner:account_detail", args=[account.pk]))
        self.assertEqual(account.account_type, AssetAccount.AccountType.LOAN)
        debt = account.debt
        self.assertEqual(debt.current_principal, Decimal("300000.00"))
        self.assertEqual(debt.annual_interest_rate, Decimal("3.20"))
        self.assertEqual(debt.monthly_payment, Decimal("1450.00"))

    def test_account_detail_shows_depot_holdings_and_related_transfer_rules(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        account = AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"),
            currency="EUR",
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=account,
            name="Vanguard FTSE All-World",
            isin="IE00B3RBWM25",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("10000.00"),
            currency="EUR",
        )
        TransferRule.objects.create(
            household=household,
            name="Depot transfer",
            amount=Decimal("500.00"),
            source_account=savings,
            target_account=account,
            start_month=date(2026, 1, 1),
        )

        response = self.client.get(reverse("planner:account_detail", args=[account.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Account detail")
        self.assertContains(response, "ING Depot")
        self.assertContains(response, "Vanguard FTSE All-World")
        self.assertContains(response, "Depot transfer")
        self.assertContains(response, "Tagesgeld -> ING Depot")
        self.assertContains(response, "Projected Account Ledger")
        self.assertContains(response, "Account Forecast")
        self.assertContains(response, "1,500.00 EUR")
        self.assertNotContains(response, "See distribution income")

        source_response = self.client.get(reverse("planner:account_detail", args=[savings.pk]))

        self.assertContains(source_response, "Depot transfer")
        self.assertContains(source_response, "Tagesgeld -> ING Depot")
        self.assertContains(source_response, "-500.00 EUR")
        self.assertContains(source_response, "Account Forecast")
        self.assertContains(source_response, "9,500.00 EUR")

    def test_account_detail_surfaces_trust_and_reconciliation_summary(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            source=AssetAccount.Source.MONEYMONEY,
            balance=Decimal("1000.00"),
            currency="EUR",
            as_of_date=date(2026, 1, 1),
        )
        MoneyRule.objects.create(
            household=household,
            name="Large rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("2000.00"),
            account=account,
            start_month=date(2026, 1, 1),
        )

        response = self.client.get(reverse("planner:account_detail", args=[account.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Trust & Reconciliation")
        self.assertContains(response, "Valuation age")
        self.assertContains(response, "Stale valuation")
        self.assertContains(response, "MoneyMoney key missing")
        self.assertContains(response, "Projected negative balance")
        self.assertContains(response, "Forecast low point")
        self.assertContains(response, "Forecast movement")
        self.assertContains(response, "-24,000.00 EUR")
        self.assertContains(response, "Movement")
        self.assertContains(response, "largest outflow")
        self.assertContains(response, "Why did this move?")
        self.assertContains(response, "Routed items")
        self.assertContains(response, "Recurring rules: 1")
        self.assertContains(response, "Open reconciliation")
        self.assertContains(response, reverse("planner:reconciliation_center") + "?q=Giro")
        self.assertContains(response, "Refresh import")
        self.assertContains(response, "Review mappings")

    def test_account_detail_surfaces_depot_reconciliation_actions(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        account = AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"),
            currency="EUR",
            depot_valuation=AssetAccount.DepotValuation.ACCOUNT_BALANCE,
            as_of_date=date(2026, 7, 1),
        )
        DepotHolding.objects.create(
            asset_account=account,
            name="Vanguard FTSE All-World",
            isin="IE00B3RBWM25",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
        )

        response = self.client.get(reverse("planner:account_detail", args=[account.pk]))

        self.assertContains(response, "Depot drift")
        self.assertContains(response, "Review holdings")
        self.assertContains(response, "Holdings minus account balance")

    def test_account_detail_explains_child_owned_excluded_accounts(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        parent = Person.objects.create(household=household, name="Parent", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Lina", role=Person.Role.CHILD)
        source = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("50000.00"),
            currency="EUR",
        )
        child_depot = AssetAccount.objects.create(
            household=household,
            name="Lina Kinderdepot",
            account_type=AssetAccount.AccountType.DEPOT,
            owner_type=AssetAccount.OwnerType.PERSON,
            owner_person=child,
            counts_in_household_net_worth=False,
            balance=Decimal("10000.00"),
            currency="EUR",
        )
        FamilyGiftPlan.objects.create(
            household=household,
            giver=parent,
            recipient=child,
            source_account=source,
            target_account=child_depot,
            name="Kinderdepot gift",
            amount=Decimal("20000.00"),
            gift_month=date(2027, 1, 1),
            purpose="Kinderdepot",
        )

        response = self.client.get(reverse("planner:account_detail", args=[child_depot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ownership & Planning Treatment")
        self.assertContains(response, "Legal owner")
        self.assertContains(response, "Lina")
        self.assertContains(response, "Child")
        self.assertContains(response, "Planning totals")
        self.assertContains(response, "Excluded")
        self.assertContains(response, "Child-owned assets are useful for tracking gifts and Kinderdepots separately.")
        self.assertContains(response, "Tracked separately from household net worth and retirement totals")
        self.assertContains(response, "Linked family gifts")
        self.assertContains(response, "Kinderdepot gift")
        self.assertContains(response, "Parent -> Lina")
        self.assertContains(response, "Tagesgeld -> Lina Kinderdepot")

    def test_account_detail_links_to_income_timeline_when_distributions_configured(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        account = AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"),
            currency="EUR",
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=account,
            name="Distributing ETF",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
            annual_distribution_rate=Decimal("2.00"),
        )

        response = self.client.get(reverse("planner:account_detail", args=[account.pk]))

        self.assertContains(response, "See distribution income")
        self.assertContains(response, reverse("planner:income_timeline") + "?source=Depot+distribution")

    def test_holding_index_shows_distribution_rate_and_cadence(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        account = AssetAccount.objects.create(
            household=household,
            name="ING Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("50000.00"),
            currency="EUR",
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=account,
            name="Accumulating fund",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
        )
        DepotHolding.objects.create(
            asset_account=account,
            name="Distributing fund",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("100.00"),
            annual_distribution_rate=Decimal("3.50"),
            distribution_cadence=AssetAccount.InterestCadence.YEARLY,
        )

        response = self.client.get(reverse("planner:holding_index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Accumulating fund")
        self.assertContains(response, "Distributing fund")
        self.assertContains(response, "3.50% yearly")

    def test_account_detail_shows_routed_money_rules(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            currency="EUR",
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
            currency="EUR",
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("5000.00"),
            currency="EUR",
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
            start_month=date(2026, 1, 1),
        )
        MoneyRule.objects.create(
            household=household,
            name="Savings interest workaround",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("10.00"),
            account=savings,
            start_month=date(2026, 1, 1),
        )

        giro_response = self.client.get(reverse("planner:account_detail", args=[giro.pk]))
        savings_response = self.client.get(reverse("planner:account_detail", args=[savings.pk]))

        self.assertContains(giro_response, "Recurring Cash Flow")
        self.assertContains(giro_response, "Salary")
        self.assertContains(giro_response, "default account")
        self.assertContains(giro_response, "4,000.00 EUR")
        self.assertContains(giro_response, "+3,000.00 EUR")
        self.assertContains(savings_response, "Savings interest workaround")
        self.assertContains(savings_response, "+10.00 EUR")
        self.assertContains(savings_response, "5,010.00 EUR")

    def test_account_detail_shows_linked_debt_plan(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Mortgage A",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("300000.00"),
            currency="EUR",
        )
        Debt.objects.create(
            household=household,
            account=account,
            name="Mortgage A repayment",
            current_principal=Decimal("300000.00"),
            annual_interest_rate=Decimal("3.20"),
            monthly_payment=Decimal("1450.00"),
        )

        response = self.client.get(reverse("planner:account_detail", args=[account.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Debt Plan")
        self.assertContains(response, "Mortgage A repayment")
        self.assertContains(response, "1,450.00 EUR")

    def test_account_delete_removes_clean_imported_local_account(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Old credit card",
            account_type=AssetAccount.AccountType.CASH,
            source=AssetAccount.Source.MONEYMONEY,
            balance=Decimal("0.00"),
            currency="EUR",
        )

        confirm = self.client.get(reverse("planner:account_delete", args=[account.pk]))
        self.assertContains(confirm, "Delete local account")
        self.assertContains(confirm, "MoneyMoney")

        response = self.client.post(reverse("planner:account_delete", args=[account.pk]))

        self.assertRedirects(response, reverse("planner:account_index"))
        self.assertFalse(AssetAccount.objects.filter(pk=account.pk).exists())

    def test_account_delete_blocks_records_that_would_cascade(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            currency="EUR",
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Imported depot",
            account_type=AssetAccount.AccountType.DEPOT,
            source=AssetAccount.Source.MONEYMONEY,
            balance=Decimal("1000.00"),
            currency="EUR",
        )
        DepotHolding.objects.create(
            asset_account=account,
            name="ETF",
            quantity=Decimal("1.000000"),
            latest_price=Decimal("1000.00"),
            currency="EUR",
        )

        response = self.client.post(reverse("planner:account_delete", args=[account.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot Delete Yet")
        self.assertContains(response, "depot holding")
        self.assertTrue(AssetAccount.objects.filter(pk=account.pk).exists())

    def test_retirement_detail_summarizes_plans(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=20,
            currency="EUR",
        )
        person = Person.objects.create(
            household=household,
            name="Alex",
            role=Person.Role.ADULT,
        )
        RetirementPlan.objects.create(
            household=household,
            person=person,
            name="Statutory pension",
            current_pension_points=Decimal("20.000"),
            expected_annual_points=Decimal("1.000"),
            pension_value_per_point=Decimal("40.00"),
            private_monthly_pension=Decimal("250.00"),
            retirement_start_month=date(2036, 1, 1),
        )
        CashGoal.objects.create(household=household, name="Retirement need", annual_amount=Decimal("36000.00"), start_year=2036)

        response = self.client.get(reverse("planner:retirement_detail"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "German pension planning")
        self.assertContains(response, "Retirement Gap")
        self.assertContains(response, "Statutory pension")
        self.assertContains(response, "Alex")
        self.assertContains(response, "1,450.00 EUR")
        self.assertContains(response, "ETF/cash draw need")
        self.assertContains(response, "Net retirement")
        self.assertContains(response, "Tax-aware draw")
        self.assertContains(response, "est. deductions")
        self.assertContains(response, "Retirement Health")
        self.assertContains(response, "Retirement tax assumptions still use defaults")

    def test_read_only_mode_blocks_planner_writes_but_not_reads(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="read_only_mode",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["read_only_mode"]["description"]},
        )

        get_response = self.client.get(reverse("planner:rule_create"))
        post_response = self.client.post(
            reverse("planner:rule_create"),
            {
                "name": "Blocked",
                "kind": MoneyRule.Kind.INCOME,
                "amount": "10.00",
                "cadence": MoneyRule.Cadence.MONTHLY,
                "start_month": "2026-01-01",
            },
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 302)
        self.assertFalse(MoneyRule.objects.filter(name="Blocked").exists())

    def test_backup_data_creates_sqlite_copy(self):
        with TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.sqlite3"
            source.write_text("sqlite-data", encoding="utf-8")
            databases = {
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": source,
                }
            }
            with patch.object(settings, "DATABASES", databases):
                call_command("backup_data", output_dir=Path(tmpdir), label="test")
            backups = list(Path(tmpdir).glob("*-test.sqlite3"))

        self.assertEqual(len(backups), 1)

    def test_backup_center_renders_operational_details(self):
        with TemporaryDirectory() as tmpdir, override_settings(BACKUP_DIR=Path(tmpdir)):
            response = self.client.get(reverse("planner:backup_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backup center")
        self.assertContains(response, "Create backup now")
        self.assertContains(response, "Restore Preview")
        self.assertContains(response, "Backup & Restore History")
        self.assertContains(response, "Database path")

    def test_backup_center_creates_manual_backup(self):
        with patch("planner.views.call_command") as backup_command:
            response = self.client.post(reverse("planner:backup_center"))

        self.assertRedirects(response, reverse("planner:backup_center"))
        backup_command.assert_called_once_with("backup_data", label="manual")
        event = BackupEvent.objects.get()
        self.assertEqual(event.action, BackupEvent.Action.BACKUP)
        self.assertEqual(event.status, BackupEvent.Status.SUCCEEDED)

    def test_backup_center_previews_lif_sqlite_backup(self):
        with TemporaryDirectory() as tmpdir, override_settings(BACKUP_DIR=Path(tmpdir)):
            backup = Path(tmpdir) / "lif-backup.sqlite3"
            with sqlite3.connect(backup) as db:
                db.execute("CREATE TABLE django_migrations (id integer primary key)")
                db.execute("CREATE TABLE planner_household (id integer primary key)")
                db.execute("CREATE TABLE planner_person (id integer primary key)")
                db.execute("CREATE TABLE planner_assetaccount (id integer primary key)")
                db.execute("CREATE TABLE planner_moneyrule (id integer primary key)")
                db.execute("CREATE TABLE planner_cashgoal (id integer primary key)")
                db.execute("INSERT INTO planner_household (id) VALUES (1)")
                db.execute("INSERT INTO planner_person (id) VALUES (1)")
                db.execute("INSERT INTO planner_person (id) VALUES (2)")

            response = self.client.get(f"{reverse('planner:backup_center')}?preview={backup.name}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "lif-backup.sqlite3")
        self.assertContains(response, "valid")
        self.assertContains(response, "Households")
        self.assertContains(response, "People")
        self.assertContains(response, "2")
        self.assertContains(response, "Restore selected backup")

    def test_restore_sqlite_backup_copies_valid_backup_and_runs_checks(self):
        from .views import restore_sqlite_backup

        with TemporaryDirectory() as tmpdir, override_settings(BACKUP_DIR=Path(tmpdir)):
            target = Path(tmpdir) / "active.sqlite3"
            backup = Path(tmpdir) / "lif-backup.sqlite3"
            target.write_text("old database", encoding="utf-8")
            with sqlite3.connect(backup) as db:
                db.execute("CREATE TABLE django_migrations (id integer primary key)")
                db.execute("CREATE TABLE planner_household (id integer primary key)")
                db.execute("INSERT INTO planner_household (id) VALUES (1)")
            databases = {
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": target,
                }
            }

            with patch.object(settings, "DATABASES", databases), patch("planner.views.call_command") as command:
                restore_sqlite_backup(backup)

            with sqlite3.connect(target) as db:
                count = db.execute("SELECT COUNT(*) FROM planner_household").fetchone()[0]

        self.assertEqual(count, 1)
        command.assert_any_call("backup_data", label="pre-restore")
        command.assert_any_call("migrate", interactive=False, verbosity=0)
        command.assert_any_call("check", verbosity=0)

    def test_backup_center_restore_requires_confirmation(self):
        with TemporaryDirectory() as tmpdir, override_settings(BACKUP_DIR=Path(tmpdir)):
            backup = Path(tmpdir) / "lif-backup.sqlite3"
            with sqlite3.connect(backup) as db:
                db.execute("CREATE TABLE django_migrations (id integer primary key)")
                db.execute("CREATE TABLE planner_household (id integer primary key)")

            with patch("planner.views.restore_sqlite_backup") as restore:
                response = self.client.post(
                    reverse("planner:backup_center"),
                    {"action": "restore", "backup_name": backup.name},
                )

        self.assertRedirects(response, reverse("planner:backup_center"))
        restore.assert_not_called()
        event = BackupEvent.objects.get()
        self.assertEqual(event.action, BackupEvent.Action.RESTORE)
        self.assertEqual(event.status, BackupEvent.Status.FAILED)

    def test_backup_center_restore_calls_restore_helper(self):
        with TemporaryDirectory() as tmpdir, override_settings(BACKUP_DIR=Path(tmpdir)):
            backup = Path(tmpdir) / "lif-backup.sqlite3"
            with sqlite3.connect(backup) as db:
                db.execute("CREATE TABLE django_migrations (id integer primary key)")
                db.execute("CREATE TABLE planner_household (id integer primary key)")

            pre_restore = Path(tmpdir) / "source-20260626-000000-pre-restore.sqlite3"
            pre_restore.write_text("backup", encoding="utf-8")

            with patch("planner.views.restore_sqlite_backup", return_value=pre_restore) as restore:
                response = self.client.post(
                    reverse("planner:backup_center"),
                    {"action": "restore", "backup_name": backup.name, "confirm_restore": "yes"},
                )

        self.assertRedirects(response, reverse("planner:system_status"))
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args[0].name, backup.name)
        event = BackupEvent.objects.get()
        self.assertEqual(event.status, BackupEvent.Status.SUCCEEDED)
        self.assertEqual(event.filename, backup.name)
        self.assertEqual(event.pre_restore_filename, pre_restore.name)

    def test_check_production_and_smoke_test_commands_run(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )

        call_command("check_production")
        call_command("smoke_test")

    def test_production_static_files_are_served_by_whitenoise(self):
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE)
        self.assertIn("staticfiles", settings.STORAGES)
        self.assertEqual(
            settings.STORAGES["staticfiles"]["BACKEND"],
            "django.contrib.staticfiles.storage.StaticFilesStorage",
        )
        self.assertFalse(settings.USE_MANIFEST_STATICFILES)
        self.assertFalse(settings.WHITENOISE_MANIFEST_STRICT)
        self.assertEqual(Path(settings.STATIC_ROOT).name, "staticfiles")

    def test_database_path_can_be_configured_for_container_volume(self):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "lif.settings",
            "DJANGO_DB_PATH": "/data/db.sqlite3",
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from django.conf import settings; print(settings.DATABASES['default']['NAME'])",
            ],
            check=True,
            capture_output=True,
            env=env,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), "/data/db.sqlite3")

    def test_home_assistant_addon_defaults_to_wildcard_allowed_hosts(self):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "lif.settings",
            "LIF_HOME_ASSISTANT_ADDON": "1",
        }
        env.pop("DJANGO_ALLOWED_HOSTS", None)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from django.conf import settings; print(','.join(settings.ALLOWED_HOSTS))",
            ],
            check=True,
            capture_output=True,
            env=env,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), "*")

    def test_home_assistant_supervisor_defaults_to_wildcard_allowed_hosts(self):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "lif.settings",
            "SUPERVISOR_TOKEN": "test-token",
        }
        env.pop("DJANGO_ALLOWED_HOSTS", None)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from django.conf import settings; print(','.join(settings.ALLOWED_HOSTS))",
            ],
            check=True,
            capture_output=True,
            env=env,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), "*")

    def test_explicit_allowed_hosts_override_home_assistant_default(self):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "lif.settings",
            "LIF_HOME_ASSISTANT_ADDON": "1",
            "DJANGO_ALLOWED_HOSTS": "lif.local,192.0.2.10",
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from django.conf import settings; print(','.join(settings.ALLOWED_HOSTS))",
            ],
            check=True,
            capture_output=True,
            env=env,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), "lif.local,192.0.2.10")

    def test_home_assistant_ingress_prefix_routes_to_health(self):
        response = self.client.get(
            "/api/hassio_ingress/test-token/health/",
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_home_assistant_ingress_prefix_is_used_for_generated_urls(self):
        Household.objects.create(
            name="Ingress Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        response = self.client.get(
            "/api/hassio_ingress/test-token/",
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/api/hassio_ingress/test-token/analytics/')
        self.assertContains(response, '/api/hassio_ingress/test-token/static/planner/app.css')
        self.assertContains(response, '/api/hassio_ingress/test-token/static/planner/app.js')

    def test_home_assistant_ingress_prefix_is_stripped_before_static_serving(self):
        from lif.middleware import HomeAssistantIngressMiddleware

        seen = {}

        def get_response(request):
            seen["path_info"] = request.path_info
            return HttpResponse("ok")

        middleware = HomeAssistantIngressMiddleware(get_response)
        request = RequestFactory().get(
            "/api/hassio_ingress/test-token/static/planner/app.css",
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
        )
        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["path_info"], "/static/planner/app.css")

    def test_home_assistant_ingress_prefix_can_be_inferred_from_path(self):
        from lif.middleware import HomeAssistantIngressMiddleware

        seen = {}

        def get_response(request):
            seen["script_name"] = request.META.get("SCRIPT_NAME")
            seen["path_info"] = request.path_info
            return HttpResponse("ok")

        middleware = HomeAssistantIngressMiddleware(get_response)
        request = RequestFactory().get("/api/hassio_ingress/test-token/static/planner/app.css")
        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["script_name"], "/api/hassio_ingress/test-token")
        self.assertEqual(seen["path_info"], "/static/planner/app.css")

    @override_settings(LIF_HOME_ASSISTANT_ADDON=True)
    def test_home_assistant_ingress_bypasses_csrf_for_post_forms(self):
        demo = Household.objects.create(
            name="Demo",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        real = Household.objects.create(
            name="Real",
            data_mode=Household.DataMode.REAL,
            starting_balance=Decimal("2000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            f"/api/hassio_ingress/test-token/household/{demo.pk}/switch/",
            {"next": "/api/hassio_ingress/test-token/"},
            HTTP_SEC_FETCH_DEST="iframe",
            HTTP_SEC_FETCH_SITE="same-origin",
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(response["Location"], "/api/hassio_ingress/test-token/")
        demo.refresh_from_db()
        real.refresh_from_db()
        self.assertTrue(demo.is_active)
        self.assertFalse(real.is_active)

    def test_home_assistant_ingress_does_not_bypass_csrf_outside_addon_mode(self):
        demo = Household.objects.create(
            name="Demo",
            data_mode=Household.DataMode.DEMO,
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        Household.objects.create(
            name="Real",
            data_mode=Household.DataMode.REAL,
            starting_balance=Decimal("2000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            f"/api/hassio_ingress/test-token/household/{demo.pk}/switch/",
            {"next": "/api/hassio_ingress/test-token/"},
            HTTP_SEC_FETCH_DEST="iframe",
            HTTP_SEC_FETCH_SITE="same-origin",
        )

        self.assertEqual(response.status_code, 403)

    def test_language_switch_under_home_assistant_ingress_does_not_require_csrf(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/api/hassio_ingress/test-token/i18n/setlang/",
            {"language": "de", "next": "/api/hassio_ingress/test-token/"},
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(
            response["Location"],
            f"/api/hassio_ingress/test-token/?{settings.LIF_LANGUAGE_QUERY_PARAM}=de",
        )
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "de")
        self.assertEqual(response.cookies[settings.LIF_LANGUAGE_COOKIE_NAME].value, "de")

    def test_language_switch_under_home_assistant_ingress_without_header_keeps_language(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/api/hassio_ingress/test-token/i18n/setlang/",
            {"language": "de", "next": "/api/hassio_ingress/test-token/"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/api/hassio_ingress/test-token/?{settings.LIF_LANGUAGE_QUERY_PARAM}=de",
        )

    def test_language_switch_under_home_assistant_ingress_survives_without_cookies(self):
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        client = Client(enforce_csrf_checks=True)

        response = client.get(
            f"/api/hassio_ingress/test-token/?{settings.LIF_LANGUAGE_QUERY_PARAM}=de",
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
            HTTP_ACCEPT_LANGUAGE="en",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<html lang="de">')
        self.assertContains(response, "Sprache")
        self.assertContains(response, 'meta name="lif-ingress-path" content="/api/hassio_ingress/test-token"')

    def test_home_assistant_ingress_keeps_selected_language_after_redirect(self):
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        client = Client(enforce_csrf_checks=True)
        client.cookies[settings.LIF_LANGUAGE_COOKIE_NAME] = "de"

        response = client.get(
            "/api/hassio_ingress/test-token/",
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
            HTTP_ACCEPT_LANGUAGE="en",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<html lang="de">')
        self.assertContains(response, "Sprache")

    def test_language_switch_under_home_assistant_ingress_rejects_ha_ui_redirects(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/api/hassio_ingress/test-token/i18n/setlang/",
            {"language": "de", "next": "/dashboard-lif/sidebar"},
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/api/hassio_ingress/test-token/?{settings.LIF_LANGUAGE_QUERY_PARAM}=de",
        )

    def test_privacy_toggle_under_home_assistant_ingress_does_not_require_csrf(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/api/hassio_ingress/test-token/privacy-mode/",
            {"enabled": "1", "next": "/api/hassio_ingress/test-token/"},
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(
            response["Location"],
            f"/api/hassio_ingress/test-token/?{settings.LIF_PRIVACY_QUERY_PARAM}=1",
        )
        self.assertTrue(client.session["privacy_mode_enabled"])

    def test_privacy_toggle_under_home_assistant_ingress_without_header(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/api/hassio_ingress/test-token/privacy-mode/",
            {"enabled": "1", "next": "/api/hassio_ingress/test-token/"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(
            response["Location"],
            f"/api/hassio_ingress/test-token/?{settings.LIF_PRIVACY_QUERY_PARAM}=1",
        )
        self.assertTrue(client.session["privacy_mode_enabled"])

    def test_privacy_mode_under_home_assistant_ingress_survives_without_session(self):
        Household.objects.create(
            name="Demo",
            starting_balance=Decimal("1234.56"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        client = Client(enforce_csrf_checks=True)

        response = client.get(
            f"/api/hassio_ingress/test-token/?{settings.LIF_PRIVACY_QUERY_PARAM}=1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "••••• EUR")
        self.assertContains(response, "On")
        self.assertNotContains(response, "1,234.56 EUR")

    def test_privacy_toggle_under_home_assistant_ingress_rejects_ha_ui_redirects(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/api/hassio_ingress/test-token/privacy-mode/",
            {"enabled": "1", "next": "/dashboard-lif/sidebar"},
            HTTP_X_INGRESS_PATH="/api/hassio_ingress/test-token",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/api/hassio_ingress/test-token/?{settings.LIF_PRIVACY_QUERY_PARAM}=1",
        )

    def test_deploy_local_collects_static_files(self):
        with patch("planner.management.commands.deploy_local.call_command") as command:
            call_command("deploy_local", skip_backup=True, skip_smoke_test=True)

        command.assert_any_call("migrate")
        command.assert_any_call("collectstatic", interactive=False, verbosity=0)
        command.assert_any_call("check_production")

    def test_deploy_local_can_skip_static_collection(self):
        with patch("planner.management.commands.deploy_local.call_command") as command:
            call_command(
                "deploy_local",
                skip_backup=True,
                skip_collectstatic=True,
                skip_smoke_test=True,
            )

        called_commands = [call.args[0] for call in command.call_args_list]
        self.assertNotIn("collectstatic", called_commands)

    def test_smoke_test_requires_household(self):
        Household.objects.all().delete()

        with self.assertRaises(CommandError):
            call_command("smoke_test")

    def test_depot_holding_create_page_renders_form(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        household = Household.objects.get()
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
        )

        response = self.client.get(reverse("planner:depot_holding_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add depot holding")
        self.assertContains(response, "For distributing ETFs")
        self.assertContains(response, "monthly averaged income rule")
        self.assertContains(response, "asset_account")
        self.assertContains(response, "latest_price")
        self.assertContains(response, "payout_date")
        self.assertContains(response, "payout_amount")

    def test_frontend_avoids_inner_html_in_app_owned_assets(self):
        planner_dir = Path(__file__).resolve().parent
        app_files = [
            *planner_dir.joinpath("templates").rglob("*.html"),
            *planner_dir.joinpath("static", "planner").rglob("*.js"),
        ]
        for path in app_files:
            with self.subTest(path=path.relative_to(planner_dir)):
                self.assertNotIn("innerHTML", path.read_text(encoding="utf-8"))

    def test_yearly_rule_only_applies_in_anchor_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=household,
            name="Insurance",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("600.00"),
            cadence=MoneyRule.Cadence.YEARLY,
            start_month=date(2026, 3, 1),
        )

        projection = build_projection(household)

        self.assertEqual(projection[1].expenses, Decimal("0.00"))
        self.assertEqual(projection[2].expenses, Decimal("600.00"))
        self.assertEqual(projection[11].expenses, Decimal("0.00"))

    def test_yearly_rule_without_start_month_anchors_to_projection_start(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 5, 1),
            planning_months=24,
        )
        MoneyRule.objects.create(
            household=household,
            name="Property tax",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("1200.00"),
            cadence=MoneyRule.Cadence.YEARLY,
            start_month=None,
        )

        projection = build_projection(household)

        # Fires once a year in the projection's start month (May), not every
        # month: index 0 = May 2026, index 12 = May 2027.
        yearly_hits = [item.index for item in projection if item.expenses]
        self.assertEqual(yearly_hits, [0, 12])
        self.assertEqual(projection[0].expenses, Decimal("1200.00"))
        self.assertEqual(projection[1].expenses, Decimal("0.00"))

    def test_depot_transfer_moves_cash_to_invested_assets(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
            capital_income_allowance=Decimal("0.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
        )
        TransferRule.objects.create(
            household=household,
            name="ETF savings plan",
            amount=Decimal("100.00"),
            target_account=depot,
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].expenses, Decimal("0.00"))
        self.assertEqual(projection[0].transfers, Decimal("100.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("400.00"))
        self.assertEqual(projection[0].invested_balance, Decimal("1100.00"))
        self.assertEqual(projection[0].net_worth, Decimal("1500.00"))

    def test_depot_transfer_from_savings_reduces_future_savings_interest(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
            capital_income_allowance=Decimal("0.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"),
            savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.QUARTERLY,
            savings_interest_tax_rate=Decimal("25.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        TransferRule.objects.create(
            household=household,
            name="ETF purchase",
            amount=Decimal("1000.00"),
            cadence=TransferRule.Cadence.YEARLY,
            source_account=savings,
            target_account=depot,
            start_month=date(2026, 1, 1),
        )

        projection = build_projection(household)
        transfer_line = next(line for line in projection[0].audit_lines if line.name == "ETF purchase")

        self.assertEqual(projection[0].liquid_balance, Decimal("11000.00"))
        self.assertEqual(projection[0].invested_balance, Decimal("1000.00"))
        self.assertEqual(projection[2].savings_interest_income, Decimal("247.50"))
        self.assertEqual(projection[2].liquid_balance, Decimal("11247.50"))
        self.assertIn("From Tagesgeld to Depot", transfer_line.note)

    def test_one_time_transfer_applies_only_in_start_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("5000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        rule = TransferRule.objects.create(
            household=household,
            name="ETF top-up",
            amount=Decimal("1000.00"),
            cadence=TransferRule.Cadence.ONCE,
            source_account=savings,
            target_account=depot,
            start_month=date(2026, 2, 1),
        )

        projection = build_projection(household)

        self.assertEqual(rule.monthly_amount, Decimal("0.00"))
        self.assertEqual(projection[0].transfers, Decimal("0.00"))
        self.assertEqual(projection[1].transfers, Decimal("1000.00"))
        self.assertEqual(projection[1].liquid_balance, Decimal("4000.00"))
        self.assertEqual(projection[1].invested_balance, Decimal("1000.00"))
        self.assertEqual(projection[2].transfers, Decimal("0.00"))

    def test_planned_investment_purchase_starts_in_purchase_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2027, 11, 1),
            planning_months=3,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("50000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="Bond buy",
            asset_type=PlannedInvestmentPurchase.AssetType.BOND,
            isin="IE0008UEVOE0",
            source_account=savings,
            target_account=depot,
            purchase_amount=Decimal("28450.00"),
            purchase_month=date(2027, 12, 1),
        )

        projection = build_projection(household)
        purchase_line = next(line for line in projection[1].audit_lines if line.section == "Planned investment purchase")

        self.assertEqual(projection[0].transfers, Decimal("0.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("50000.00"))
        self.assertEqual(projection[0].invested_balance, Decimal("0.00"))
        self.assertEqual(projection[1].transfers, Decimal("28450.00"))
        self.assertEqual(projection[1].liquid_balance, Decimal("21550.00"))
        self.assertEqual(projection[1].invested_balance, Decimal("28450.00"))
        self.assertEqual(projection[1].account_balances[savings.id], Decimal("21550.00"))
        self.assertEqual(projection[1].account_balances[depot.id], Decimal("28450.00"))
        self.assertIn("IE0008UEVOE0", purchase_line.note)

    def test_planned_bond_purchase_pays_out_at_maturity(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2027, 12, 1),
            planning_months=14,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("30000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        household.default_operating_account = giro
        household.save()
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="2029 target bond",
            asset_type=PlannedInvestmentPurchase.AssetType.BOND,
            source_account=savings,
            target_account=depot,
            purchase_amount=Decimal("28450.00"),
            purchase_month=date(2027, 12, 1),
            payout_date=date(2029, 1, 1),
            payout_amount=Decimal("30000.00"),
        )

        projection = build_projection(household)
        payout_month = projection[13]
        payout_line = next(line for line in payout_month.audit_lines if line.section == "Planned investment payout")

        self.assertEqual(projection[0].invested_balance, Decimal("28450.00"))
        self.assertEqual(payout_month.depot_payout, Decimal("30000.00"))
        self.assertEqual(payout_month.liquid_balance, Decimal("32550.00"))
        self.assertEqual(payout_month.invested_balance, Decimal("0.00"))
        self.assertEqual(payout_month.account_balances[giro.id], Decimal("31000.00"))
        self.assertEqual(payout_month.account_balances[depot.id], Decimal("0.00"))
        self.assertIn("expected return since purchase 1550.00", payout_line.note)

    def test_planned_investment_payout_taxes_gain_over_purchase_amount(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2027, 12, 1),
            planning_months=14,
            capital_gains_tax_rate=Decimal("25.00"),
            capital_income_allowance=Decimal("0.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Savings",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("30000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Bond depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
            depot_teilfreistellung_rate=Decimal("0.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="2029 target bond",
            asset_type=PlannedInvestmentPurchase.AssetType.BOND,
            source_account=savings,
            target_account=depot,
            purchase_amount=Decimal("28450.00"),
            purchase_month=date(2027, 12, 1),
            payout_date=date(2029, 1, 1),
            payout_amount=Decimal("30000.00"),
        )

        projection = build_projection(household)
        payout_month = projection[13]
        payout_line = next(line for line in payout_month.audit_lines if line.section == "Planned investment payout")

        # 1550 EUR gain taxed at 25%; the 28450 EUR principal return is untaxed.
        self.assertEqual(payout_month.depot_payout, Decimal("29612.50"))
        self.assertEqual(payout_month.liquid_balance, Decimal("32162.50"))
        self.assertEqual(payout_month.account_balances[giro.id], Decimal("30612.50"))
        self.assertEqual(payout_line.cash_effect, Decimal("29612.50"))
        self.assertIn("1550.00 taxable gain", payout_line.note)
        self.assertIn("387.50 capital tax", payout_line.note)

    def test_transfer_plan_shows_events_and_source_account_warnings(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("500.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        rule = TransferRule.objects.create(
            household=household,
            name="ETF top-up",
            amount=Decimal("1000.00"),
            cadence=TransferRule.Cadence.ONCE,
            source_account=savings,
            target_account=depot,
            start_month=date(2026, 1, 1),
        )

        response = self.client.get(reverse("planner:transfer_plan"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cash Flow Ledger")
        self.assertContains(response, "Scheduled Transfers")
        self.assertContains(response, "Transfer Rules")
        self.assertContains(response, "ETF top-up")
        self.assertContains(response, "Tagesgeld")
        self.assertContains(response, "Depot")
        self.assertContains(response, "overdraws Tagesgeld")
        self.assertContains(response, reverse("planner:transfer_rule_update", args=[rule.pk]))
        self.assertContains(response, "Edit")
        self.assertContains(response, "-500.00 EUR")

    def test_transfer_plan_shows_edit_for_unscheduled_future_rule(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("5000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        rule = TransferRule.objects.create(
            household=household,
            name="Future ETF top-up",
            amount=Decimal("1000.00"),
            cadence=TransferRule.Cadence.ONCE,
            source_account=savings,
            target_account=depot,
            start_month=date(2027, 1, 1),
        )

        response = self.client.get(reverse("planner:transfer_plan"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Future ETF top-up")
        self.assertContains(response, reverse("planner:transfer_rule_update", args=[rule.pk]))
        self.assertContains(response, "No scheduled transfer events in the current projection horizon.")

    def test_transfer_plan_ledger_search_filters_cash_movements(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("5000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )
        household.default_operating_account = giro
        household.save()
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("4000.00"),
            start_month=date(2026, 1, 1),
        )
        TransferRule.objects.create(
            household=household,
            name="ETF top-up",
            amount=Decimal("1000.00"),
            cadence=TransferRule.Cadence.ONCE,
            source_account=giro,
            target_account=depot,
            start_month=date(2026, 1, 1),
        )

        response = self.client.get(reverse("planner:transfer_plan"), {"q": "ETF top-up"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="ETF top-up"')
        self.assertContains(response, "ETF top-up")
        self.assertNotContains(response, "Salary")

    def test_cash_flow_ledger_covers_income_expense_debt_and_transfers(self):
        from planner.forecast_explain import cash_flow_ledger_rows

        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=2
        )
        giro = AssetAccount.objects.create(
            household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("50000.00")
        )
        household.default_operating_account = giro
        household.save()
        loan_acc = AssetAccount.objects.create(
            household=household, name="Mortgage", account_type=AssetAccount.AccountType.LOAN, balance=Decimal("100000.00")
        )
        Debt.objects.create(
            household=household, account=loan_acc, name="Mortgage", current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"), monthly_payment=Decimal("1000.00"),
        )
        MoneyRule.objects.create(household=household, name="Salary", kind=MoneyRule.Kind.INCOME, amount=Decimal("4000.00"), start_month=date(2026, 1, 1))
        MoneyRule.objects.create(household=household, name="Rent", kind=MoneyRule.Kind.EXPENSE, amount=Decimal("1500.00"), start_month=date(2026, 1, 1))

        rows = cash_flow_ledger_rows(build_projection(household))
        sections = {row["section"] for row in rows}

        # The ledger surfaces everything, not just transfers.
        self.assertIn("Income rule", sections)
        self.assertIn("Expense rule", sections)
        self.assertIn("Debt", sections)  # the regular mortgage payment now appears
        # Rules and the debt payment all route through the operating account.
        self.assertTrue(any(r["section"] == "Income rule" and r["account_id"] == giro.id for r in rows))
        self.assertTrue(any(r["section"] == "Debt" and r["account_id"] == giro.id for r in rows))

    def test_account_ledger_paginates_by_calendar_year(self):
        from planner.forecast_explain import account_ledger_rows, account_ledger_years

        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 7, 1), planning_years=2
        )
        giro = AssetAccount.objects.create(
            household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("10000.00")
        )
        household.default_operating_account = giro
        household.save()
        MoneyRule.objects.create(
            household=household, name="Salary", kind=MoneyRule.Kind.INCOME, amount=Decimal("3000.00"), start_month=date(2026, 7, 1)
        )

        projection = build_projection(household)

        # July 2026 over two years spans three calendar years.
        self.assertEqual(account_ledger_years(projection, giro), [2026, 2027, 2028])
        rows_2027 = account_ledger_rows(projection, giro, year=2027)
        self.assertTrue(rows_2027)
        self.assertTrue(all(row["month"].year == 2027 for row in rows_2027))
        # No filter returns the full horizon (more than one year's worth).
        self.assertGreater(len(account_ledger_rows(projection, giro)), len(rows_2027))

    def test_projection_exports_csv(self):
        household = Household.objects.create(
            name="Export Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        CashGoal.objects.create(
            household=household,
            name="Yearly need",
            annual_amount=Decimal("24000.00"),
            start_year=2026,
        )

        monthly = self.client.get(reverse("planner:projection_monthly_export"))
        yearly = self.client.get(reverse("planner:projection_yearly_export"))

        self.assertEqual(monthly.status_code, 200)
        self.assertEqual(monthly["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('filename="export-test-projection-monthly.csv"', monthly["Content-Disposition"])
        self.assertIn("month,income", monthly.content.decode())
        self.assertIn("2026-01,3000.00", monthly.content.decode())
        self.assertEqual(yearly.status_code, 200)
        yearly_text = yearly.content.decode()
        self.assertIn("year,label,month_count", yearly_text)
        self.assertIn("2026,2026,12,36000.00", yearly_text)

    def test_cash_flow_and_statement_exports_csv(self):
        household = Household.objects.create(
            name="T",
            starting_balance=Decimal("500.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"),
            account=giro,
        )

        cash_flow = self.client.get(reverse("planner:cash_flow_export"))
        account_statement = self.client.get(reverse("planner:account_statement_export", args=[giro.pk]))

        self.assertEqual(cash_flow.status_code, 200)
        self.assertIn("month,year,section,group,name,account,amount,note,detail_index", cash_flow.content.decode())
        self.assertIn("Giro,1000.00", cash_flow.content.decode())
        self.assertEqual(account_statement.status_code, 200)
        self.assertIn("Giro,Income rule,Salary,1000.00", account_statement.content.decode())

    def test_general_pool_statement_export_csv(self):
        household = Household.objects.create(
            name="T",
            starting_balance=Decimal("500.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        MoneyRule.objects.create(
            household=household,
            name="Unrouted bonus",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("250.00"),
        )

        cash_flow = self.client.get(reverse("planner:cash_flow_export"))
        pool_statement = self.client.get(reverse("planner:general_pool_statement_export"))

        self.assertEqual(cash_flow.status_code, 200)
        self.assertIn("General liquid pool", cash_flow.content.decode())
        self.assertEqual(pool_statement.status_code, 200)
        self.assertIn("General liquid pool,Income rule,Unrouted bonus,250.00", pool_statement.content.decode())

    def test_yearly_report_and_slide_deck_render(self):
        household = Household.objects.create(
            name="Report Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_years=10,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Sam", role=Person.Role.CHILD)
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
        )
        CashGoal.objects.create(
            household=household,
            name="Need",
            annual_amount=Decimal("30000.00"),
            start_year=2026,
        )
        ChildMilestone.objects.create(
            person=child,
            name="Leaves school",
            start_month=date(2030, 8, 1),
            monthly_cost_delta=Decimal("-250.00"),
        )
        RetirementPlan.objects.create(
            household=household,
            person=adult,
            name="Pension",
            current_pension_points=Decimal("20.0000"),
            private_monthly_pension=Decimal("1200.00"),
            retirement_start_month=date(2032, 1, 1),
        )

        report = self.client.get(reverse("planner:yearly_report"), {"year": 2026})
        slides = self.client.get(reverse("planner:yearly_report_slides", args=[2026]))

        self.assertEqual(report.status_code, 200)
        self.assertContains(report, "2026")
        self.assertContains(report, "Planning Drivers")
        self.assertContains(report, reverse("planner:yearly_report_slides", args=[2026]))
        self.assertContains(report, "Monthly Appendix")
        self.assertEqual(slides.status_code, 200)
        self.assertContains(slides, "Our Financial Journey")
        self.assertContains(slides, "1. Where do we start?")
        self.assertContains(slides, "2. What is next year?")
        self.assertContains(slides, "3. What is in five years?")
        self.assertContains(slides, "4. Which major changes happen?")
        self.assertContains(slides, "5. What does retirement look like?")
        self.assertContains(slides, "Leaves school")
        self.assertContains(slides, "Retirement starts around")
        self.assertContains(slides, "style=\"--w:")

    def test_projection_audit_month_picker_and_account_balances(self):
        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=12
        )
        AssetAccount.objects.create(
            household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("5000.00")
        )

        # ?goto jumps to that month regardless of the path index.
        response = self.client.get(reverse("planner:projection_audit", args=[0]), {"goto": "2026-06"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "June 2026")
        # The per-account balances panel lists the account as of that month.
        self.assertContains(response, "Account Balances")
        self.assertContains(response, "Giro")

    def test_general_pool_statement_shows_unrouted_cash_flows(self):
        Household.objects.create(
            name="T",
            starting_balance=Decimal("500.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        household = Household.objects.get()
        MoneyRule.objects.create(
            household=household,
            name="Unrouted salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("1000.00"),
        )

        response = self.client.get(reverse("planner:general_pool_detail"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "General liquid pool")
        self.assertContains(response, "Unrouted salary")
        self.assertContains(response, "+1,000.00 EUR")
        self.assertContains(response, "1,500.00 EUR")

    def test_projection_year_audit_year_picker_and_account_balances(self):
        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_years=3
        )
        AssetAccount.objects.create(
            household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("5000.00")
        )

        response = self.client.get(reverse("planner:projection_year_audit", args=[0]), {"goto_year": "2028"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Year forecast detail")
        self.assertContains(response, "Account Balances")
        self.assertContains(response, "Giro")
        # The jump landed on 2028 (its monthly breakdown shows Dec 2028).
        self.assertContains(response, "2028")

    def test_depot_expected_return_compounds_monthly(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=2,
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1200.00"),
            depot_annual_return_rate=Decimal("12.00"),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].investment_income, Decimal("0.00"))
        self.assertEqual(projection[0].depot_growth, Decimal("11.39"))
        self.assertEqual(projection[0].invested_balance, Decimal("1211.39"))
        self.assertEqual(projection[1].depot_growth, Decimal("11.49"))
        self.assertEqual(projection[1].invested_balance, Decimal("1222.88"))
        self.assertEqual(projection[0].audit_lines[0].section, "Depot growth")

    def test_depot_expected_return_and_transfer_share_depot_balance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=2,
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1200.00"),
            depot_annual_return_rate=Decimal("12.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
        )
        TransferRule.objects.create(
            household=household,
            name="ETF savings plan",
            amount=Decimal("100.00"),
            target_account=depot,
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].investment_income, Decimal("0.00"))
        self.assertEqual(projection[0].depot_growth, Decimal("11.39"))
        self.assertEqual(projection[0].transfers, Decimal("100.00"))
        self.assertEqual(projection[0].invested_balance, Decimal("1311.39"))
        self.assertEqual(projection[1].depot_growth, Decimal("12.44"))
        self.assertEqual(projection[1].invested_balance, Decimal("1423.83"))

    def test_depot_can_use_summed_holdings_as_projection_value(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
            capital_income_allowance=Decimal("0.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("1000.00"),
            depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="MSCI World",
            quantity=Decimal("10.000000"),
            latest_price=Decimal("150.00"),
        )
        DepotHolding.objects.create(
            asset_account=depot,
            name="MSCI EM",
            quantity=Decimal("5.000000"),
            latest_price=Decimal("80.00"),
        )

        projection = build_projection(household)

        self.assertEqual(depot.holdings_value, Decimal("1900.00000000"))
        # The stored balance is kept in sync with holdings, so there is no drift.
        self.assertEqual(depot.depot_difference, Decimal("0.00"))
        depot.refresh_from_db()
        self.assertEqual(depot.balance, Decimal("1900.00"))
        self.assertEqual(projection[0].invested_balance, Decimal("1900.00000000"))
        self.assertEqual(projection[0].net_worth, Decimal("1900.00000000"))

    def test_holdings_keep_depot_balance_in_sync(self):
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=1
        )
        depot = AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"), depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        a = DepotHolding.objects.create(asset_account=depot, name="A", quantity=Decimal("10.000000"), latest_price=Decimal("100.00"))
        b = DepotHolding.objects.create(asset_account=depot, name="B", quantity=Decimal("5.000000"), latest_price=Decimal("80.00"))

        depot.refresh_from_db()
        self.assertEqual(depot.balance, Decimal("1400.00"))  # 1000 + 400

        b.delete()
        depot.refresh_from_db()
        self.assertEqual(depot.balance, Decimal("1000.00"))

        a.latest_price = Decimal("120.00")
        a.save()
        depot.refresh_from_db()
        self.assertEqual(depot.balance, Decimal("1200.00"))

    def test_account_balance_depot_keeps_stated_balance(self):
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=1
        )
        depot = AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("5000.00"), depot_valuation=AssetAccount.DepotValuation.ACCOUNT_BALANCE,
        )
        DepotHolding.objects.create(asset_account=depot, name="ETF", quantity=Decimal("1.000000"), latest_price=Decimal("900.00"))

        depot.refresh_from_db()
        # Account-balance valuation keeps the stated balance; holdings are informational.
        self.assertEqual(depot.balance, Decimal("5000.00"))

    def test_savings_transfer_stays_inside_liquid_balance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
            capital_income_allowance=Decimal("0.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("1000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("500.00"),
        )
        TransferRule.objects.create(
            household=household,
            name="Emergency fund transfer",
            amount=Decimal("100.00"),
            target_account=savings,
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].transfers, Decimal("0.00"))
        self.assertEqual(projection[0].net, Decimal("0.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("1500.00"))
        self.assertEqual(projection[0].net_worth, Decimal("1500.00"))

    def test_transfer_rule_form_supports_source_account_and_rejects_same_target(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("1000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )

        response = self.client.get(reverse("planner:transfer_rule_create"))

        self.assertContains(response, "Source account")
        self.assertContains(response, "Tagesgeld")

        invalid_response = self.client.post(
            reverse("planner:transfer_rule_create"),
            {
                "name": "Bad transfer",
                "source_account": str(savings.pk),
                "target_account": str(savings.pk),
                "amount": "100.00",
                "cadence": TransferRule.Cadence.MONTHLY,
                "person": "",
                "category": "Investing",
                "start_month": "2026-06-01",
                "end_month": "",
                "is_active": "on",
                "notes": "",
            },
        )

        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, "Source and target accounts must be different")

        missing_start_response = self.client.post(
            reverse("planner:transfer_rule_create"),
            {
                "name": "One-time ETF buy",
                "source_account": str(savings.pk),
                "target_account": str(depot.pk),
                "amount": "100.00",
                "cadence": TransferRule.Cadence.ONCE,
                "person": "",
                "category": "Investing",
                "start_month": "",
                "end_month": "",
                "is_active": "on",
                "notes": "",
            },
        )

        self.assertEqual(missing_start_response.status_code, 200)
        self.assertContains(missing_start_response, "Set the month when this one-time transfer should happen.")

        valid_response = self.client.post(
            reverse("planner:transfer_rule_create"),
            {
                "name": "ETF transfer",
                "source_account": str(savings.pk),
                "target_account": str(depot.pk),
                "amount": "100.00",
                "cadence": TransferRule.Cadence.MONTHLY,
                "person": "",
                "category": "Investing",
                "start_month": "2026-06-01",
                "end_month": "",
                "is_active": "on",
                "notes": "",
            },
        )
        rule = TransferRule.objects.get(name="ETF transfer")

        self.assertRedirects(valid_response, reverse("planner:plan_index"))
        self.assertEqual(rule.source_account, savings)
        self.assertEqual(rule.target_account, depot)

    def test_planned_investment_purchase_form_validates_and_creates_purchase(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("30000.00"),
        )
        depot = AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"),
        )

        response = self.client.get(reverse("planner:planned_investment_purchase_create"))

        self.assertContains(response, "Add planned investment purchase")
        self.assertContains(response, "Tagesgeld")
        self.assertContains(response, "Depot")

        invalid_response = self.client.post(
            reverse("planner:planned_investment_purchase_create"),
            {
                "name": "Bad bond",
                "asset_type": PlannedInvestmentPurchase.AssetType.BOND,
                "isin": "IE0008UEVOE0",
                "ticker": "",
                "source_account": str(savings.pk),
                "target_account": str(depot.pk),
                "purchase_amount": "28450.00",
                "purchase_month": "2027-12",
                "payout_date": "2027-01-01",
                "payout_amount": "30000.00",
                "annual_distribution_rate": "0.00",
                "distribution_cadence": AssetAccount.InterestCadence.QUARTERLY,
                "person": "",
                "is_active": "on",
                "notes": "",
            },
        )

        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, "Payout date must be on or after the purchase month.")

        valid_response = self.client.post(
            reverse("planner:planned_investment_purchase_create"),
            {
                "name": "Target bond",
                "asset_type": PlannedInvestmentPurchase.AssetType.BOND,
                "isin": "IE0008UEVOE0",
                "ticker": "",
                "source_account": str(savings.pk),
                "target_account": str(depot.pk),
                "purchase_amount": "28450.00",
                "purchase_month": "2027-12",
                "payout_date": "2029-01-01",
                "payout_amount": "30000.00",
                "annual_distribution_rate": "0.00",
                "distribution_cadence": AssetAccount.InterestCadence.QUARTERLY,
                "person": "",
                "is_active": "on",
                "notes": "",
            },
        )
        purchase = PlannedInvestmentPurchase.objects.get(name="Target bond")

        self.assertRedirects(valid_response, reverse("planner:plan_index"))
        self.assertEqual(purchase.source_account, savings)
        self.assertEqual(purchase.target_account, depot)
        self.assertEqual(purchase.purchase_month, date(2027, 12, 1))

    def test_savings_interest_adds_net_income_after_simple_tax(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
            capital_income_allowance=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"),
            savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.MONTHLY,
            savings_interest_tax_rate=Decimal("25.00"),
        )

        projection = build_projection(household)
        lines = {line.name: line for line in projection[0].audit_lines}

        self.assertEqual(projection[0].savings_interest_income, Decimal("90.00"))
        self.assertEqual(projection[0].income, Decimal("90.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("12090.00"))
        self.assertEqual(lines["Tagesgeld"].cash_effect, Decimal("90.00"))
        self.assertEqual(lines["Tagesgeld"].note, "120.00 gross, 0.00 allowance, 30.00 tax at 25.00%")

    def test_savings_interest_uses_capital_income_allowance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
            capital_income_allowance=Decimal("2000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"),
            savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.MONTHLY,
            savings_interest_tax_rate=Decimal("25.00"),
        )

        projection = build_projection(household)
        lines = {line.name: line for line in projection[0].audit_lines}

        self.assertEqual(projection[0].savings_interest_income, Decimal("120.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("12120.00"))
        self.assertEqual(lines["Tagesgeld"].note, "120.00 gross, 120.00 allowance, 0.00 tax at 25.00%")

    def test_quarterly_savings_interest_waits_for_payout_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=3,
            capital_income_allowance=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("12000.00"),
            savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.QUARTERLY,
            savings_interest_tax_rate=Decimal("25.00"),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].savings_interest_income, Decimal("0.00"))
        self.assertEqual(projection[1].savings_interest_income, Decimal("0.00"))
        self.assertEqual(projection[2].savings_interest_income, Decimal("270.00"))

    def test_negative_loan_account_balance_counts_as_positive_liability(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        AssetAccount.objects.create(
            household=household,
            name="Imported mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("-100000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("10000.00"),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].opening_liability_balance, Decimal("100000.00"))
        self.assertEqual(projection[0].net_worth, Decimal("-90000.00"))

    def test_debt_payment_splits_interest_and_principal(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("10000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("12.00"),
            monthly_payment=Decimal("1100.00"),
            start_month=date(2026, 6, 1),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].debt_interest, Decimal("1000.00"))
        self.assertEqual(projection[0].debt_principal, Decimal("100.00"))
        self.assertEqual(projection[0].expenses, Decimal("1000.00"))
        self.assertEqual(projection[0].transfers, Decimal("100.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("8900.00"))
        self.assertEqual(projection[0].liability_balance, Decimal("99900.00"))
        self.assertEqual(projection[0].net_worth, Decimal("-91000.00"))

    def test_debt_refinance_terms_apply_after_fixed_interest_period(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("10000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("12.00"),
            monthly_payment=Decimal("1100.00"),
            start_month=date(2026, 1, 1),
            fixed_interest_until=date(2026, 1, 1),
            refinance_annual_interest_rate=Decimal("6.00"),
            refinance_monthly_payment=Decimal("900.00"),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].debt_interest, Decimal("1000.00"))
        self.assertEqual(projection[0].debt_principal, Decimal("100.00"))
        self.assertEqual(projection[1].debt_interest, Decimal("499.50"))
        self.assertEqual(projection[1].debt_principal, Decimal("400.50"))
        self.assertEqual(projection[2].liability_balance, Decimal("99097.00"))

    def test_extra_repayment_rule_reduces_amortizing_principal(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=2,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("20000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("12.00"),
            monthly_payment=Decimal("1100.00"),
            start_month=date(2026, 6, 1),
        )
        TransferRule.objects.create(
            household=household,
            name="Sondertilgung",
            amount=Decimal("5000.00"),
            target_account=mortgage,
        )

        projection = build_projection(household)

        # Month 0: 1000 interest + 100 scheduled principal, then 5000 extra ->
        # principal 100000 - 100 - 5000 = 94900, liability matches.
        self.assertEqual(projection[0].debt_principal, Decimal("5100.00"))
        self.assertEqual(projection[0].liability_balance, Decimal("94900.00"))
        # Month 1 interest is charged on the reduced balance: 94900 * 1% = 949.
        self.assertEqual(projection[1].debt_interest, Decimal("949.00"))

    def test_extra_repayment_is_capped_at_outstanding_balance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("500.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("20000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("500.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("100.00"),
            start_month=date(2026, 6, 1),
        )
        TransferRule.objects.create(
            household=household,
            name="Clear it",
            amount=Decimal("5000.00"),
            target_account=mortgage,
        )

        projection = build_projection(household)

        # 100 scheduled payment leaves 400; the 5000 extra only applies 400.
        self.assertEqual(projection[0].liability_balance, Decimal("0.00"))
        # Only 100 (scheduled) + 400 (capped extra) = 500 leaves the Giro.
        self.assertEqual(projection[0].liquid_balance, Decimal("19500.00"))

    def test_debt_save_syncs_linked_loan_account_balance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        debt = Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("90000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"),
        )
        mortgage.refresh_from_db()
        self.assertEqual(mortgage.balance, Decimal("90000.00"))

        debt.current_principal = Decimal("80000.00")
        debt.save()
        mortgage.refresh_from_db()
        self.assertEqual(mortgage.balance, Decimal("80000.00"))

    def test_debt_clean_rejects_payment_below_interest(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        debt = Debt(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("12.00"),  # first-month interest is 1000
            monthly_payment=Decimal("500.00"),
        )
        with self.assertRaises(ValidationError) as ctx:
            debt.full_clean()
        self.assertIn("monthly_payment", ctx.exception.error_dict)

    def test_debt_form_groups_fields_and_explains_fixed_interest_payoff(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("-250000.00"),
        )

        response = self.client.get(reverse("planner:debt_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Loan Balance")
        self.assertContains(response, "Current Terms")
        self.assertContains(response, "After Fixed Interest")
        self.assertContains(response, "Payment account")
        self.assertContains(response, "For a full payoff when the fixed rate ends")
        self.assertContains(response, "The projection caps the final payment at the balance owed")
        self.assertContains(response, "Mortgage - 100000.00 EUR (principal 100000.00 EUR)")
        self.assertContains(response, "Mortgage - -250000.00 EUR (principal 250000.00 EUR)")
        self.assertContains(response, 'data-balance="100000.00"')
        self.assertContains(response, 'data-principal="250000.00"')

    def test_debt_form_defaults_blank_principal_from_selected_account_balance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("123456.78"),
        )

        response = self.client.post(
            reverse("planner:debt_create"),
            {
                "account": mortgage.pk,
                "name": "Mortgage repayment",
                "current_principal": "",
                "annual_interest_rate": "3.00",
                "monthly_payment": "1000.00",
                "start_month": "2026-06-01",
                "annual_extra_payment": "0.00",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("planner:debt_index"))
        debt = household.debts.get()
        self.assertEqual(debt.current_principal, Decimal("123456.78"))

    def test_debt_form_defaults_blank_principal_from_negative_account_balance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("-123456.78"),
        )

        response = self.client.post(
            reverse("planner:debt_create"),
            {
                "account": mortgage.pk,
                "name": "Mortgage repayment",
                "current_principal": "",
                "annual_interest_rate": "3.00",
                "monthly_payment": "1000.00",
                "start_month": "2026-06-01",
                "annual_extra_payment": "0.00",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("planner:debt_index"))
        debt = household.debts.get()
        self.assertEqual(debt.current_principal, Decimal("123456.78"))

    def test_debt_form_saves_source_account(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Mortgage buffer",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("20000.00"),
        )

        response = self.client.post(
            reverse("planner:debt_create"),
            {
                "account": mortgage.pk,
                "source_account": savings.pk,
                "name": "Mortgage repayment",
                "current_principal": "100000.00",
                "annual_interest_rate": "3.00",
                "monthly_payment": "1000.00",
                "start_month": "2026-06-01",
                "annual_extra_payment": "0.00",
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("planner:debt_index"))
        debt = household.debts.get()
        self.assertEqual(debt.source_account, savings)

    def test_debt_clean_requires_both_refinance_fields(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        debt = Debt(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"),
            refinance_annual_interest_rate=Decimal("4.00"),  # payment missing
        )
        with self.assertRaises(ValidationError) as ctx:
            debt.full_clean()
        self.assertIn("refinance_monthly_payment", ctx.exception.error_dict)

    def test_summarize_debt_reports_payoff_and_total_interest(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Loan",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("1000.00"),
        )
        debt = Debt.objects.create(
            household=household,
            account=mortgage,
            name="Loan",
            current_principal=Decimal("1000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("100.00"),
            start_month=date(2026, 1, 1),
        )

        summary = summarize_debt(debt, date(2026, 1, 1))

        # 1000 at 0% interest, 100/month -> paid off in 10 months, no interest.
        self.assertEqual(summary["months_to_payoff"], 10)
        self.assertEqual(summary["payoff_month"], date(2026, 10, 1))
        self.assertEqual(summary["total_interest"], Decimal("0.00"))
        self.assertEqual(summary["ending_principal"], Decimal("0.00"))

    def test_quality_warns_when_debt_does_not_amortize_by_end_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Loan",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Loan",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("500.00"),
            start_month=date(2026, 1, 1),
            end_month=date(2027, 1, 1),
        )

        report = build_quality_report(household)
        titles = {item.title for item in report["issues"]}

        self.assertIn("Loan does not fully amortize by its end month", titles)

    def test_interest_only_window_keeps_principal_flat_then_amortizes(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("20000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("12.00"),  # 1% per month -> 1000 interest
            monthly_payment=Decimal("1100.00"),
            start_month=date(2026, 1, 1),
            interest_only_until=date(2026, 3, 1),
        )

        projection = build_projection(household)

        # Jan + Feb are interest-only: pay 1000 interest, principal unchanged.
        self.assertEqual(projection[0].debt_interest, Decimal("1000.00"))
        self.assertEqual(projection[0].debt_principal, Decimal("0.00"))
        self.assertEqual(projection[0].liability_balance, Decimal("100000.00"))
        self.assertEqual(projection[1].debt_principal, Decimal("0.00"))
        # March amortizes normally: 1000 interest + 100 principal.
        self.assertEqual(projection[2].debt_interest, Decimal("1000.00"))
        self.assertEqual(projection[2].debt_principal, Decimal("100.00"))
        self.assertEqual(projection[2].liability_balance, Decimal("99900.00"))

    def test_annual_extra_payment_reduces_principal_on_anchor_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=4,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("50000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("0.00"),  # 0% keeps the arithmetic exact
            monthly_payment=Decimal("100.00"),
            start_month=date(2026, 1, 1),
            annual_extra_payment=Decimal("6000.00"),
            extra_payment_month=3,
        )

        projection = build_projection(household)

        # Only March (month index 2) carries the extra repayment.
        self.assertEqual(projection[0].debt_principal, Decimal("100.00"))
        self.assertEqual(projection[1].debt_principal, Decimal("100.00"))
        self.assertEqual(projection[2].debt_principal, Decimal("6100.00"))  # 100 scheduled + 6000 extra
        lines = {line.section for line in projection[2].audit_lines}
        self.assertIn("Extra repayment", lines)
        # End of March balance: 100000 - 100 - 100 - 100 - 6000 = 93700.
        self.assertEqual(projection[2].liability_balance, Decimal("93700.00"))
        self.assertEqual(projection[3].debt_principal, Decimal("100.00"))

    def test_summarize_debt_accounts_for_extra_payments(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Loan",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("12000.00"),
        )
        debt = Debt.objects.create(
            household=household,
            account=mortgage,
            name="Loan",
            current_principal=Decimal("12000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("100.00"),
            start_month=date(2026, 1, 1),
            annual_extra_payment=Decimal("6000.00"),
            extra_payment_month=1,
        )

        summary = summarize_debt(debt, date(2026, 1, 1))

        # Jan 2026: 100 scheduled + 6000 extra -> 5900; 100/month through Dec
        # (-1100) -> 4800; Jan 2027 repeats the yearly extra and clears it.
        # Payoff lands on the 13th payment.
        self.assertEqual(summary["months_to_payoff"], 13)
        self.assertEqual(summary["total_interest"], Decimal("0.00"))

    def test_debt_detail_page_renders_schedule(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="debts",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["debts"]["description"]},
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("1000.00"),
        )
        debt = Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("1000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("100.00"),
            start_month=date(2026, 1, 1),
        )

        response = self.client.get(reverse("planner:debt_detail", args=[debt.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Amortization schedule")
        self.assertContains(response, "2026-10")  # payoff month appears in the schedule

    def test_debt_clean_rejects_invalid_extra_payment_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        debt = Debt(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"),
            extra_payment_month=13,
        )
        with self.assertRaises(ValidationError) as ctx:
            debt.full_clean()
        self.assertIn("extra_payment_month", ctx.exception.error_dict)

    def test_interest_only_window_still_applies_sondertilgung(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=4,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("12000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("50000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("12000.00"),
            annual_interest_rate=Decimal("0.00"),  # 0% keeps arithmetic exact
            monthly_payment=Decimal("100.00"),
            start_month=date(2026, 1, 1),
            interest_only_until=date(2026, 4, 1),  # Jan-Mar interest only
            annual_extra_payment=Decimal("1000.00"),
            extra_payment_month=1,
        )

        projection = build_projection(household)

        # Jan is interest-only (no scheduled principal) but the yearly extra
        # repayment still reduces the balance.
        self.assertEqual(projection[0].debt_principal, Decimal("1000.00"))
        self.assertEqual(projection[0].liability_balance, Decimal("11000.00"))
        # Feb still interest-only and not the extra month -> balance flat.
        self.assertEqual(projection[1].debt_principal, Decimal("0.00"))
        self.assertEqual(projection[1].liability_balance, Decimal("11000.00"))
        # April amortizes normally.
        self.assertEqual(projection[3].debt_principal, Decimal("100.00"))

    def test_interest_only_uses_refinance_rate_when_active(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("50000.00"),
        )
        Debt.objects.create(
            household=household,
            account=mortgage,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("12.00"),  # 1%/month
            monthly_payment=Decimal("1100.00"),
            start_month=date(2026, 1, 1),
            fixed_interest_until=date(2026, 1, 1),  # refinance from Feb
            refinance_annual_interest_rate=Decimal("6.00"),  # 0.5%/month
            refinance_monthly_payment=Decimal("900.00"),
            interest_only_until=date(2026, 3, 1),  # Jan-Feb interest only
        )

        projection = build_projection(household)

        # Jan: original rate, interest only.
        self.assertEqual(projection[0].debt_interest, Decimal("1000.00"))
        self.assertEqual(projection[0].debt_principal, Decimal("0.00"))
        # Feb: refinanced rate, still interest only.
        self.assertEqual(projection[1].debt_interest, Decimal("500.00"))
        self.assertEqual(projection[1].debt_principal, Decimal("0.00"))
        # Mar: refinanced rate, now amortizing (900 payment - 500 interest).
        self.assertEqual(projection[2].debt_interest, Decimal("500.00"))
        self.assertEqual(projection[2].debt_principal, Decimal("400.00"))

    def test_debt_integrity_handles_negative_amortization_from_refinance_assumption(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        mortgage = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("100000.00"),
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("50000.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        Debt.objects.create(
            household=household,
            account=mortgage,
            source_account=giro,
            name="Mortgage",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("1.20"),
            monthly_payment=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            fixed_interest_until=date(2026, 1, 1),
            refinance_annual_interest_rate=Decimal("12.00"),
            refinance_monthly_payment=Decimal("100.00"),
        )

        projection = build_projection(household)
        result = check_projection_integrity(projection, accounts=household.accounts.all())
        debt_line = next(line for line in projection[1].audit_lines if line.section == "Debt")

        self.assertEqual(projection[1].debt_interest, Decimal("991.00"))
        self.assertEqual(projection[1].debt_principal, Decimal("0.00"))
        self.assertEqual(projection[1].liability_balance, Decimal("99991.00"))
        self.assertEqual(debt_line.liability_effect, Decimal("891.00"))
        self.assertTrue(result["ok"], result["failures"])

    def test_child_milestone_applies_income_and_cost(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        child = Person.objects.create(household=household, name="Lina", role=Person.Role.CHILD)
        ChildMilestone.objects.create(
            person=child,
            name="Daycare",
            start_month=date(2026, 6, 1),
            monthly_income_delta=Decimal("200.00"),
            monthly_cost_delta=Decimal("500.00"),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].child_income, Decimal("200.00"))
        self.assertEqual(projection[0].child_expenses, Decimal("500.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("-300.00"))

    def test_scenario_applies_one_time_and_recurring_deltas(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=2,
        )
        scenario = Scenario.objects.create(
            household=household,
            name="What if",
            liquid_balance_delta=Decimal("500.00"),
            monthly_income_delta=Decimal("100.00"),
            monthly_expense_delta=Decimal("30.00"),
        )

        projection = build_projection(household, scenario=scenario)

        # 1000 start + 500 one-time, then +70/month.
        self.assertEqual(projection[0].scenario_income, Decimal("100.00"))
        self.assertEqual(projection[0].scenario_expenses, Decimal("30.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("1570.00"))
        # One-time delta is not reapplied in month 2.
        self.assertEqual(projection[1].liquid_balance, Decimal("1640.00"))

    def test_planned_row_for_current_month_picks_most_recent_not_future(self):
        current = date.today().replace(day=1)
        past = date(current.year - 1, current.month, 1)
        future = date(current.year + 1, current.month, 1)
        summary = {
            "projection": {
                "monthly": [
                    {"month": past.isoformat(), "label": "past"},
                    {"month": current.isoformat(), "label": "current"},
                    {"month": future.isoformat(), "label": "future"},
                ]
            }
        }

        self.assertEqual(planned_row_for_current_month(summary)["label"], "current")

    def test_planned_row_for_current_month_falls_back_to_earliest_future(self):
        current = date.today().replace(day=1)
        near = date(current.year + 1, current.month, 1)
        far = date(current.year + 2, current.month, 1)
        summary = {
            "projection": {
                "monthly": [
                    {"month": far.isoformat(), "label": "far"},
                    {"month": near.isoformat(), "label": "near"},
                ]
            }
        }

        self.assertEqual(planned_row_for_current_month(summary)["label"], "near")

    def test_moneymoney_discover_then_preview_applies_override_and_disable(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        FeatureFlag.objects.update_or_create(
            key="moneymoney_import",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["moneymoney_import"]["description"]},
        )

        with patch(
            "planner.views.MoneyMoneyConnector",
            return_value=MoneyMoneyConnector(client=FakeMoneyMoneyClient()),
        ):
            # 1. Discover stores selectable source accounts (no batch).
            self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_discover_accounts"})

            tagesgeld = MoneyMoneyAccountMapping.objects.get(
                household=household, source_key="account:index:1:Tagesgeld"
            )
            tagesgeld.account_type = AssetAccount.AccountType.SAVINGS
            tagesgeld.save()
            depot = MoneyMoneyAccountMapping.objects.get(
                household=household, source_key="portfolio:index:0:ING Depot"
            )
            depot.import_enabled = False
            depot.save()

            # 2. Preview applies the override and skips the disabled source.
            self.client.post(reverse("planner:import_center"), {"import_kind": "moneymoney_accounts"})

        self.assertEqual(MoneyMoneyAccountMapping.objects.filter(household=household).count(), 3)
        batch = ImportBatch.objects.get(summary__import_kind="moneymoney_accounts")
        by_name = {row["values"]["name"]: row["values"] for row in batch.summary["rows"]}
        self.assertNotIn("ING Depot", by_name)  # disabled portfolio excluded
        self.assertEqual(by_name["Tagesgeld"]["account_type"], AssetAccount.AccountType.SAVINGS)
        self.assertEqual(by_name["Giro"]["account_type"], AssetAccount.AccountType.CASH)

    def test_income_investment_applies_between_start_and_end_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 6, 1),
            planning_months=4,
        )
        IncomeInvestment.objects.create(
            household=household,
            name="Solar",
            investment_type=IncomeInvestment.InvestmentType.SOLAR,
            principal=Decimal("10000.00"),
            monthly_income=Decimal("125.00"),
            start_month=date(2026, 7, 1),
            end_month=date(2026, 8, 1),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].investment_income, Decimal("0.00"))
        self.assertEqual(projection[1].investment_income, Decimal("125.00"))
        self.assertEqual(projection[2].investment_income, Decimal("125.00"))
        self.assertEqual(projection[3].investment_income, Decimal("0.00"))
        self.assertEqual(projection[2].liquid_balance, Decimal("1250.00"))

    def test_future_income_investment_deducts_principal_from_funding_account(self):
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 6, 1), planning_months=12
        )
        savings = AssetAccount.objects.create(
            household=household, name="Tagesgeld", account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("50000.00"),
        )
        IncomeInvestment.objects.create(
            household=household, name="PV roof", investment_type=IncomeInvestment.InvestmentType.SOLAR,
            principal=Decimal("30000.00"), source_account=savings, monthly_income=Decimal("200.00"),
            annual_growth_rate=Decimal("0.00"), start_month=date(2026, 8, 1), end_month=date(2046, 12, 1),
        )

        projection = build_projection(household)

        # Before install: full savings, no outlay yet.
        self.assertEqual(projection[0].net_worth, Decimal("50000.00"))
        self.assertEqual(projection[1].net_worth, Decimal("50000.00"))
        # Aug (index 2): principal leaves savings; +200 income that month; no asset booked.
        august = projection[2]
        self.assertEqual(august.month, date(2026, 8, 1))
        self.assertEqual(august.liquid_balance, Decimal("20200.00"))  # 50000 - 30000 + 200
        self.assertEqual(august.net_worth, Decimal("20200.00"))
        sections = {line.section: line for line in august.audit_lines}
        self.assertIn("Investment purchase", sections)
        self.assertEqual(sections["Investment purchase"].cash_effect, Decimal("-30000.00"))
        # Sep: income only, no second deduction.
        self.assertEqual(projection[3].liquid_balance, Decimal("20400.00"))

    def test_already_owned_income_investment_is_not_deducted(self):
        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 6, 1), planning_months=12
        )
        savings = AssetAccount.objects.create(
            household=household, name="Tagesgeld", account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("50000.00"),
        )
        IncomeInvestment.objects.create(
            household=household, name="Old PV", investment_type=IncomeInvestment.InvestmentType.SOLAR,
            principal=Decimal("30000.00"), source_account=savings, monthly_income=Decimal("200.00"),
            annual_growth_rate=Decimal("0.00"), start_month=date(2026, 1, 1), end_month=date(2046, 12, 1),
        )

        projection = build_projection(household)

        # Started before the projection -> outlay already happened, not re-deducted.
        sections = {line.section for line in projection[0].audit_lines}
        self.assertNotIn("Investment purchase", sections)
        self.assertEqual(projection[0].liquid_balance, Decimal("50200.00"))  # savings + income, no -30k

    def test_income_rules_can_use_household_default_growth(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=14,
            default_income_growth_rate=Decimal("10.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("100.00"),
            start_month=date(2026, 1, 1),
        )
        MoneyRule.objects.create(
            household=household,
            name="Rent",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("50.00"),
            start_month=date(2026, 1, 1),
        )

        projection = build_projection(household)
        grown_line = next(line for line in projection[12].audit_lines if line.name == "Salary")

        self.assertEqual(projection[0].income_rule_income, Decimal("100.00"))
        self.assertEqual(projection[11].income_rule_income, Decimal("100.00"))
        self.assertEqual(projection[12].income_rule_income, Decimal("110.00"))
        self.assertEqual(projection[12].expenses, Decimal("50.00"))
        self.assertIn("10.00% annual income growth", grown_line.note)

    def test_money_rules_route_through_explicit_or_default_account(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("500.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        MoneyRule.objects.create(
            household=household,
            name="Salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3000.00"),
            start_month=date(2026, 1, 1),
        )
        MoneyRule.objects.create(
            household=household,
            name="Groceries",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("700.00"),
            start_month=date(2026, 1, 1),
        )
        MoneyRule.objects.create(
            household=household,
            name="Savings interest workaround",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("10.00"),
            account=savings,
            start_month=date(2026, 1, 1),
        )

        projection = build_projection(household)
        first_month = projection[0]
        lines = {line.name: line for line in first_month.audit_lines}

        self.assertEqual(first_month.liquid_balance, Decimal("3810.00"))
        self.assertEqual(first_month.account_balances[giro.id], Decimal("3300.00"))
        self.assertEqual(first_month.account_balances[savings.id], Decimal("510.00"))
        self.assertIn("to Giro", lines["Salary"].note)
        self.assertIn("from Giro", lines["Groceries"].note)
        self.assertIn("to Tagesgeld", lines["Savings interest workaround"].note)

    def test_debt_payments_use_source_account_when_set(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("5000.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Mortgage buffer",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("1000.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        loan_account = AssetAccount.objects.create(
            household=household,
            name="Mortgage",
            account_type=AssetAccount.AccountType.LOAN,
            balance=Decimal("1000.00"),
        )
        Debt.objects.create(
            household=household,
            account=loan_account,
            source_account=savings,
            name="Mortgage",
            current_principal=Decimal("1000.00"),
            annual_interest_rate=Decimal("0.00"),
            monthly_payment=Decimal("100.00"),
            start_month=date(2026, 1, 1),
            annual_extra_payment=Decimal("200.00"),
            extra_payment_month=1,
        )

        first_month = build_projection(household)[0]
        lines = {line.section: line for line in first_month.audit_lines if line.name == "Mortgage"}

        self.assertEqual(first_month.account_balances[giro.id], Decimal("5000.00"))
        self.assertEqual(first_month.account_balances[savings.id], Decimal("700.00"))
        self.assertEqual(first_month.liability_balance, Decimal("700.00"))
        self.assertIn("from Mortgage buffer", lines["Debt"].note)
        self.assertIn("from Mortgage buffer", lines["Extra repayment"].note)

    def test_income_rule_growth_can_be_overridden_to_flat(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=13,
            default_income_growth_rate=Decimal("10.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Kindergeld",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("255.00"),
            annual_growth_rate=Decimal("0.00"),
            start_month=date(2026, 1, 1),
        )

        projection = build_projection(household)

        self.assertEqual(projection[12].income_rule_income, Decimal("255.00"))

    def test_income_investment_growth_uses_item_override(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=13,
            default_income_growth_rate=Decimal("10.00"),
        )
        IncomeInvestment.objects.create(
            household=household,
            name="Solar",
            investment_type=IncomeInvestment.InvestmentType.SOLAR,
            principal=Decimal("10000.00"),
            monthly_income=Decimal("100.00"),
            annual_growth_rate=Decimal("5.00"),
            start_month=date(2026, 1, 1),
            end_month=date(2027, 12, 1),
        )

        projection = build_projection(household)
        grown_line = next(line for line in projection[12].audit_lines if line.name == "Solar")

        self.assertEqual(projection[0].investment_income, Decimal("100.00"))
        self.assertEqual(projection[12].investment_income, Decimal("105.00"))
        self.assertIn("5.00% annual income growth", grown_line.note)

    def test_income_investment_form_uses_month_inputs(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 6, 1),
            planning_months=4,
        )
        investment = IncomeInvestment.objects.create(
            household=household,
            name="Solar",
            investment_type=IncomeInvestment.InvestmentType.SOLAR,
            principal=Decimal("10000.00"),
            monthly_income=Decimal("125.00"),
            start_month=date(2026, 7, 1),
            end_month=date(2026, 8, 1),
        )

        response = self.client.get(reverse("planner:income_investment_update", args=[investment.pk]))

        self.assertContains(response, 'name="start_month"')
        self.assertContains(response, 'type="month"')
        self.assertContains(response, 'value="2026-07"')
        self.assertContains(response, 'name="duration_years"')
        self.assertContains(response, "Principal is the invested or tied-up capital")
        self.assertContains(response, "For irregular solar payments, use a monthly average")

        response = self.client.post(
            reverse("planner:income_investment_update", args=[investment.pk]),
            {
                "name": "Solar",
                "investment_type": IncomeInvestment.InvestmentType.SOLAR,
                "principal": "10000.00",
                "monthly_income": "130.00",
                "annual_growth_rate": "1.50",
                "currency": "EUR",
                "start_month": "2026-09",
                "end_month": "2026-10",
                "is_active": "on",
                "notes": "",
            },
        )
        investment.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(investment.start_month, date(2026, 9, 1))
        self.assertEqual(investment.end_month, date(2026, 10, 1))
        self.assertEqual(investment.annual_growth_rate, Decimal("1.50"))

    def test_income_investment_duration_sets_end_month(self):
        Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 6, 1),
            planning_months=4,
        )

        response = self.client.post(
            reverse("planner:income_investment_create"),
            {
                "name": "Solar",
                "investment_type": IncomeInvestment.InvestmentType.SOLAR,
                "principal": "10000.00",
                "monthly_income": "125.00",
                "annual_growth_rate": "",
                "currency": "EUR",
                "start_month": "2026-07",
                "duration_years": "20",
                "end_month": "",
                "is_active": "on",
                "notes": "",
            },
        )

        self.assertRedirects(response, reverse("planner:plan_index"))
        investment = IncomeInvestment.objects.get()
        self.assertEqual(investment.start_month, date(2026, 7, 1))
        self.assertEqual(investment.end_month, date(2046, 6, 1))

    def test_private_loan_receivable_moves_principal_and_counts_interest(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            name="Family loan",
            borrower="Sibling",
            current_principal=Decimal("1000.00"),
            monthly_interest_income=Decimal("10.00"),
            monthly_principal_repayment=Decimal("400.00"),
            start_month=date(2026, 1, 1),
        )

        projection = build_projection(household)
        first_lines = {line.section: line for line in projection[0].audit_lines}

        self.assertEqual(projection[0].opening_other_asset_balance, Decimal("1000.00"))
        self.assertEqual(projection[0].income, Decimal("10.00"))
        self.assertEqual(projection[0].investment_income, Decimal("10.00"))
        self.assertEqual(projection[0].private_loan_principal, Decimal("400.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("1410.00"))
        self.assertEqual(projection[0].other_asset_balance, Decimal("600.00"))
        self.assertEqual(projection[0].net_worth, Decimal("2010.00"))
        self.assertEqual(first_lines["Private loan interest"].cash_effect, Decimal("10.00"))
        self.assertEqual(first_lines["Private loan principal"].other_asset_effect, Decimal("-400.00"))
        self.assertEqual(projection[2].private_loan_principal, Decimal("200.00"))
        self.assertEqual(projection[2].other_asset_balance, Decimal("0.00"))

    def test_future_private_loan_not_counted_until_disbursed(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=12,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("50000.00"),
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            name="Loan to sibling",
            borrower="Sibling",
            source_account=savings,
            current_principal=Decimal("20000.00"),
            monthly_principal_repayment=Decimal("0.00"),
            disbursement_month=date(2026, 8, 1),  # future payout
            is_active=True,
        )

        # Dashboard today: only the 50k savings counts, not the not-yet-lent loan.
        summary = build_snapshot_summary(household)
        self.assertEqual(summary["totals"]["other_assets"], "0.00")
        self.assertEqual(summary["totals"]["net_worth"], "50000.00")

        projection = build_projection(household)
        # Month 0 (Jun) and Jul: money still in savings, no receivable.
        self.assertEqual(projection[0].liquid_balance, Decimal("50000.00"))
        self.assertEqual(projection[0].other_asset_balance, Decimal("0.00"))
        self.assertEqual(projection[0].net_worth, Decimal("50000.00"))
        self.assertEqual(projection[1].net_worth, Decimal("50000.00"))
        # Aug (index 2): disbursed — cash out, receivable in, net worth unchanged.
        august = projection[2]
        self.assertEqual(august.month, date(2026, 8, 1))
        self.assertEqual(august.liquid_balance, Decimal("30000.00"))
        self.assertEqual(august.other_asset_balance, Decimal("20000.00"))
        self.assertEqual(august.net_worth, Decimal("50000.00"))
        sections = {line.section: line for line in august.audit_lines}
        self.assertIn("Private loan disbursed", sections)
        self.assertEqual(sections["Private loan disbursed"].cash_effect, Decimal("-20000.00"))

    def test_already_lent_private_loan_counts_immediately(self):
        # Blank disbursement_month keeps the legacy behaviour: the principal is a
        # receivable from the start (the source balance is assumed already reduced).
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("30000.00"),
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            name="Old loan",
            borrower="Sibling",
            current_principal=Decimal("20000.00"),
            monthly_principal_repayment=Decimal("0.00"),
            disbursement_month=None,
            is_active=True,
        )

        summary = build_snapshot_summary(household)
        self.assertEqual(summary["totals"]["other_assets"], "20000.00")
        self.assertEqual(summary["totals"]["net_worth"], "50000.00")
        self.assertEqual(build_projection(household)[0].other_asset_balance, Decimal("20000.00"))

    def test_private_loan_gift_reduces_net_worth_at_payout(self):
        # A gift (is_gift) is money you do not get back: it leaves net worth when
        # paid out instead of becoming a receivable, and never accrues/repays.
        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 6, 1), planning_months=12
        )
        savings = AssetAccount.objects.create(
            household=household, name="Tagesgeld", account_type=AssetAccount.AccountType.SAVINGS, balance=Decimal("50000.00")
        )
        PrivateLoanReceivable.objects.create(
            household=household, name="Kinderheim", borrower="Children's home", source_account=savings,
            current_principal=Decimal("20000.00"), monthly_principal_repayment=Decimal("0.00"),
            disbursement_month=date(2026, 8, 1), is_gift=True, is_active=True,
        )

        projection = build_projection(household)

        # Before payout: full savings, no receivable.
        self.assertEqual(projection[0].net_worth, Decimal("50000.00"))
        self.assertEqual(projection[0].other_asset_balance, Decimal("0.00"))
        # Aug (index 2): the gift is paid out — cash leaves, NOT booked as a
        # receivable, so net worth drops by the principal and stays down.
        august = projection[2]
        self.assertEqual(august.month, date(2026, 8, 1))
        self.assertEqual(august.liquid_balance, Decimal("30000.00"))
        self.assertEqual(august.other_asset_balance, Decimal("0.00"))
        self.assertEqual(august.net_worth, Decimal("30000.00"))
        self.assertIn("Private loan gift", {line.section for line in august.audit_lines})
        self.assertEqual(projection[-1].net_worth, Decimal("30000.00"))

    def test_account_totals_excludes_future_private_loan(self):
        # Mirrors the real-data case (ACME/Kinderheim): a future-disbursed loan
        # funded from savings must not be double-counted in "now" net worth — the
        # cash is still in the savings account until it is lent.
        from planner.views import account_totals

        household = Household.objects.create(
            name="Test", starting_balance=Decimal("0.00"), start_month=date(2026, 7, 1), planning_months=12
        )
        AssetAccount.objects.create(
            household=household, name="Tagesgeld", account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("544615.78"),
        )
        PrivateLoanReceivable.objects.create(
            household=household, name="Kinderheim", borrower="X",
            current_principal=Decimal("100000.00"), disbursement_month=date(2026, 9, 1), is_active=True,
        )

        totals = account_totals(list(household.accounts.all()), household)
        self.assertEqual(totals["other_asset_total"], Decimal("0.00"))
        self.assertEqual(totals["net_worth"], Decimal("544615.78"))

        # An already-lent loan (blank disbursement_month) is still counted.
        PrivateLoanReceivable.objects.create(
            household=household, name="Old", borrower="Y",
            current_principal=Decimal("20000.00"), disbursement_month=None, is_active=True,
        )
        totals2 = account_totals(list(household.accounts.all()), household)
        self.assertEqual(totals2["other_asset_total"], Decimal("20000.00"))

    def test_balance_sheet_paths_agree(self):
        # Guardrail: the three balance-sheet computations (dashboard/account_totals,
        # snapshot totals, and the projection's opening month) must agree, so the
        # private-loan / asset-counting rules can't drift across them again.
        from planner.balance_sheet import current_balance_sheet
        from planner.finance import money_value

        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 7, 1), planning_months=1
        )
        AssetAccount.objects.create(household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("10000.00"))
        AssetAccount.objects.create(household=household, name="Tagesgeld", account_type=AssetAccount.AccountType.SAVINGS, balance=Decimal("20000.00"))
        depot = AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("0.00"), depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        )
        DepotHolding.objects.create(asset_account=depot, name="ETF", quantity=Decimal("50.000000"), latest_price=Decimal("100.00"))
        AssetAccount.objects.create(household=household, name="Art", account_type=AssetAccount.AccountType.OTHER, balance=Decimal("3000.00"))
        loan_acc = AssetAccount.objects.create(household=household, name="Mortgage", account_type=AssetAccount.AccountType.LOAN, balance=Decimal("50000.00"))
        Debt.objects.create(
            household=household, account=loan_acc, name="Mortgage", current_principal=Decimal("50000.00"),
            annual_interest_rate=Decimal("3.00"), monthly_payment=Decimal("500.00"),
        )
        PrivateLoanReceivable.objects.create(household=household, name="Lent", borrower="A", current_principal=Decimal("8000.00"), disbursement_month=None, is_active=True)
        PrivateLoanReceivable.objects.create(household=household, name="Future", borrower="B", current_principal=Decimal("100000.00"), disbursement_month=date(2026, 9, 1), is_active=True)

        bs = current_balance_sheet(household)
        summary = build_snapshot_summary(household)
        opening = build_projection(household)[0]

        # Liquid 30k, invested 5k, other 3k + 8k receivable (future 100k excluded),
        # liability 50k, net worth -4k.
        self.assertEqual(bs["liquid_total"], Decimal("30000.00"))
        self.assertEqual(bs["invested_total"], Decimal("5000.00"))
        self.assertEqual(bs["other_asset_total"], Decimal("11000.00"))
        self.assertEqual(bs["liability_total"], Decimal("50000.00"))
        self.assertEqual(bs["net_worth"], Decimal("-4000.00"))

        # Snapshot totals agree.
        self.assertEqual(summary["totals"]["liquid"], money_value(bs["liquid_total"]))
        self.assertEqual(summary["totals"]["other_assets"], money_value(bs["other_asset_total"]))
        self.assertEqual(summary["totals"]["net_worth"], money_value(bs["net_worth"]))

        # Projection opening month agrees.
        self.assertEqual(opening.opening_liquid_balance, bs["liquid_total"])
        self.assertEqual(opening.opening_invested_balance, bs["invested_total"])
        self.assertEqual(opening.opening_other_asset_balance, bs["other_asset_total"])
        self.assertEqual(opening.opening_liability_balance, bs["liability_total"])
        self.assertEqual(opening.opening_net_worth, bs["net_worth"])

    def test_child_owned_depot_is_tracked_but_excluded_from_household_net_worth(self):
        from planner.balance_sheet import current_balance_sheet
        from planner.finance import money_value

        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 7, 1), planning_months=2
        )
        child = Person.objects.create(
            household=household,
            name="Child",
            role=Person.Role.CHILD,
            birth_date=date(2016, 1, 1),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        child_depot = AssetAccount.objects.create(
            household=household,
            name="Child depot",
            account_type=AssetAccount.AccountType.DEPOT,
            owner_type=AssetAccount.OwnerType.PERSON,
            owner_person=child,
            counts_in_household_net_worth=False,
            balance=Decimal("10000.00"),
            depot_annual_return_rate=Decimal("12.00"),
        )

        bs = current_balance_sheet(household)
        summary = build_snapshot_summary(household)
        projection = build_projection(household)

        self.assertEqual(bs["liquid_total"], Decimal("1000.00"))
        self.assertEqual(bs["invested_total"], Decimal("0.00"))
        self.assertEqual(bs["net_worth"], Decimal("1000.00"))
        self.assertEqual(summary["totals"]["invested"], money_value(Decimal("0.00")))
        self.assertEqual(summary["totals"]["net_worth"], money_value(Decimal("1000.00")))
        self.assertEqual(projection[0].opening_invested_balance, Decimal("0.00"))
        self.assertEqual(projection[0].opening_net_worth, Decimal("1000.00"))
        self.assertEqual(projection[0].net_worth, Decimal("1000.00"))
        self.assertGreater(projection[0].account_balances[child_depot.id], Decimal("10000.00"))
        self.assertEqual(projection[0].invested_balance, Decimal("0.00"))

    def test_family_gift_moves_cash_to_child_account_outside_household_net_worth(self):
        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 7, 1), planning_months=2
        )
        parent = Person.objects.create(household=household, name="Parent", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Child", role=Person.Role.CHILD)
        cash = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("50000.00"),
        )
        child_depot = AssetAccount.objects.create(
            household=household,
            name="Child depot",
            account_type=AssetAccount.AccountType.DEPOT,
            owner_type=AssetAccount.OwnerType.PERSON,
            owner_person=child,
            counts_in_household_net_worth=False,
            balance=Decimal("0.00"),
        )
        FamilyGiftPlan.objects.create(
            household=household,
            name="Kinderdepot gift",
            giver=parent,
            recipient=child,
            source_account=cash,
            target_account=child_depot,
            amount=Decimal("10000.00"),
            gift_month=date(2026, 8, 1),
            purpose="Kinderdepot",
        )

        projection = build_projection(household)
        august = projection[1]

        self.assertEqual(projection[0].net_worth, Decimal("50000.00"))
        self.assertEqual(august.liquid_balance, Decimal("40000.00"))
        self.assertEqual(august.account_balances[cash.id], Decimal("40000.00"))
        self.assertEqual(august.account_balances[child_depot.id], Decimal("10000.00"))
        self.assertEqual(august.invested_balance, Decimal("0.00"))
        self.assertEqual(august.net_worth, Decimal("40000.00"))
        gift_line = next(line for line in august.audit_lines if line.section == "Family gift")
        self.assertEqual(gift_line.name, "Kinderdepot gift")
        self.assertIn("Parent to Child", gift_line.note)

    def test_plan_page_shows_family_gifts_and_allowance_windows(self):
        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=12
        )
        parent = Person.objects.create(household=household, name="Parent", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Child", role=Person.Role.CHILD)
        child_depot = AssetAccount.objects.create(
            household=household,
            name="Child depot",
            account_type=AssetAccount.AccountType.DEPOT,
            owner_type=AssetAccount.OwnerType.PERSON,
            owner_person=child,
            counts_in_household_net_worth=False,
            balance=Decimal("0.00"),
        )
        FamilyGiftPlan.objects.create(
            household=household,
            name="First gift",
            giver=parent,
            recipient=child,
            target_account=child_depot,
            amount=Decimal("100000.00"),
            gift_month=date(2026, 1, 1),
        )
        FamilyGiftPlan.objects.create(
            household=household,
            name="Second gift",
            giver=parent,
            recipient=child,
            target_account=child_depot,
            amount=Decimal("50000.00"),
            gift_month=date(2029, 1, 1),
        )

        response = self.client.get(reverse("planner:plan_index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "First gift")
        self.assertContains(response, "Family gift")
        self.assertContains(response, "Gift Allowance Windows")
        self.assertContains(response, "150,000.00 EUR")
        self.assertContains(response, "250,000.00 EUR")

    def test_snapshot_summary_captures_family_gifts_and_child_account_ownership(self):
        household = Household.objects.create(
            name="T", starting_balance=Decimal("0.00"), start_month=date(2026, 1, 1), planning_months=1
        )
        parent = Person.objects.create(household=household, name="Parent", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Child", role=Person.Role.CHILD)
        child_depot = AssetAccount.objects.create(
            household=household,
            name="Child depot",
            account_type=AssetAccount.AccountType.DEPOT,
            owner_type=AssetAccount.OwnerType.PERSON,
            owner_person=child,
            counts_in_household_net_worth=False,
            balance=Decimal("1000.00"),
        )
        FamilyGiftPlan.objects.create(
            household=household,
            name="Gift",
            giver=parent,
            recipient=child,
            target_account=child_depot,
            amount=Decimal("1000.00"),
            gift_month=date(2026, 1, 1),
        )

        summary = build_snapshot_summary(household)

        account_row = next(row for row in summary["accounts"] if row["name"] == "Child depot")
        self.assertEqual(account_row["owner_type"], AssetAccount.OwnerType.PERSON)
        self.assertEqual(account_row["owner_person"], "Child")
        self.assertFalse(account_row["counts_in_household_net_worth"])
        self.assertEqual(summary["counts"]["family_gift_plans"], 1)
        self.assertEqual(summary["family_gift_plans"][0]["name"], "Gift")
        self.assertEqual(summary["family_gift_plans"][0]["recipient"], "Child")

    def test_private_loan_calculates_net_interest_from_rate_and_tax(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_years=3,
            capital_income_allowance=Decimal("0.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            name="Three-year family loan",
            borrower="Sibling",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("4.00"),
            interest_tax_rate=Decimal("25.00"),
            monthly_principal_repayment=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            end_month=date(2028, 12, 1),
        )

        projection = build_projection(household)
        yearly = build_yearly_projection(projection)

        self.assertEqual(projection[0].income, Decimal("250.00"))
        self.assertEqual(projection[0].investment_income, Decimal("250.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("1250.00"))
        self.assertEqual(projection[0].other_asset_balance, Decimal("100000.00"))
        self.assertEqual(yearly[0].investment_income, Decimal("3000.00"))
        self.assertEqual(yearly[1].investment_income, Decimal("3000.00"))
        self.assertEqual(yearly[2].investment_income, Decimal("3000.00"))

    def test_private_loan_interest_uses_capital_income_allowance(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
            capital_income_allowance=Decimal("2000.00"),
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            name="Family loan",
            borrower="Sibling",
            current_principal=Decimal("100000.00"),
            annual_interest_rate=Decimal("4.00"),
            interest_tax_rate=Decimal("25.00"),
            monthly_principal_repayment=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            end_month=date(2026, 12, 1),
        )

        month = build_projection(household)[0]
        line = next(line for line in month.audit_lines if line.section == "Private loan interest")

        self.assertEqual(month.investment_income, Decimal("333.33"))
        self.assertIn("333.33 gross, 333.33 allowance, 0.00 tax", line.note)

    def test_private_loan_final_repayment_returns_to_source_savings(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("0.00"),
            savings_annual_interest_rate=Decimal("12.00"),
            savings_interest_cadence=AssetAccount.InterestCadence.MONTHLY,
            savings_interest_tax_rate=Decimal("0.00"),
        )
        PrivateLoanReceivable.objects.create(
            household=household,
            source_account=savings,
            name="Balloon family loan",
            borrower="Sibling",
            current_principal=Decimal("1000.00"),
            monthly_interest_income=Decimal("10.00"),
            monthly_principal_repayment=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            end_month=date(2026, 1, 1),
        )

        projection = build_projection(household)
        lines = {line.section: line for line in projection[0].audit_lines}

        self.assertEqual(projection[0].income, Decimal("20.10"))
        self.assertEqual(projection[0].savings_interest_income, Decimal("10.10"))
        self.assertEqual(projection[0].private_loan_principal, Decimal("1000.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("1020.10"))
        self.assertEqual(projection[0].other_asset_balance, Decimal("0.00"))
        self.assertIn("To Tagesgeld", lines["Private loan interest"].note)
        self.assertIn("Final repayment", lines["Private loan principal"].note)

    def test_private_loan_form_uses_month_inputs_and_plan_page_lists_it(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("5000.00"),
        )

        response = self.client.post(
            reverse("planner:private_loan_create"),
            {
                "name": "Family bridge loan",
                "borrower": "Parent",
                "source_account": str(account.pk),
                "current_principal": "5000.00",
                "annual_interest_rate": "4.00",
                "interest_tax_rate": "25.00",
                "monthly_interest_income": "25.00",
                "monthly_principal_repayment": "250.00",
                "currency": "EUR",
                "start_month": "2026-03",
                "end_month": "2027-10",
                "is_active": "on",
                "notes": "Free-form family loan",
            },
        )

        self.assertRedirects(response, reverse("planner:plan_index"))
        loan = PrivateLoanReceivable.objects.get()
        self.assertEqual(loan.start_month, date(2026, 3, 1))
        self.assertEqual(loan.end_month, date(2027, 10, 1))
        self.assertEqual(loan.source_account, account)

        edit_response = self.client.get(reverse("planner:private_loan_update", args=[loan.pk]))
        self.assertContains(edit_response, 'name="start_month"')
        self.assertContains(edit_response, 'type="month"')
        self.assertContains(edit_response, 'value="2026-03"')
        self.assertContains(edit_response, "Repayment account")
        self.assertContains(edit_response, "Annual Zins")
        self.assertContains(edit_response, "Interest tax rate")
        self.assertContains(edit_response, "Monthly Tilgung")

        plan_response = self.client.get(reverse("planner:plan_index"))
        self.assertContains(plan_response, "Family bridge loan")
        self.assertContains(plan_response, "4.00% interest")
        self.assertContains(plan_response, "to Tagesgeld")

    def test_retirement_plan_income_starts_at_retirement_month(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=4,
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        RetirementPlan.objects.create(
            household=household,
            person=person,
            name="Pension",
            current_pension_points=Decimal("10.000"),
            expected_annual_points=Decimal("1.000"),
            pension_value_per_point=Decimal("40.00"),
            private_monthly_pension=Decimal("100.00"),
            retirement_start_month=date(2026, 3, 1),
            annual_adjustment_rate=Decimal("0.00"),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].retirement_income, Decimal("0.00"))
        self.assertEqual(projection[1].retirement_income, Decimal("0.00"))
        # Net of the default 29% deduction (18% pension tax + 11% health):
        # gross 506.67 * 0.71 = 359.74.
        self.assertEqual(projection[2].retirement_income, Decimal("359.74"))
        self.assertEqual(projection[3].retirement_income, Decimal("359.74"))

    def test_pension_is_netted_in_projection_without_double_deducting(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_years=1,
            pension_tax_rate=Decimal("10.00"),
            health_insurance_rate=Decimal("10.00"),  # 20% total deduction
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
            capital_gains_tax_rate=Decimal("25.00"),
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        RetirementPlan.objects.create(
            household=household,
            person=person,
            name="Pension",
            current_pension_points=Decimal("10.000"),
            expected_annual_points=Decimal("0.000"),
            pension_value_per_point=Decimal("100.00"),  # gross 1000/month
            retirement_start_month=date(2026, 1, 1),
            annual_adjustment_rate=Decimal("0.00"),
        )

        projection = build_projection(household)
        yearly = build_yearly_projection(projection, household.cash_goals.all())

        # 1000 gross -> 800 net per month hits liquid/net worth.
        self.assertEqual(projection[0].retirement_income, Decimal("800.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("800.00"))

        # The tax-aware summary reads the net value back without deducting again.
        summary = retirement_tax_summary(yearly[0], household)
        self.assertEqual(summary["net_retirement_income"], Decimal("9600.00"))  # 800 * 12
        self.assertEqual(summary["net_income"], Decimal("9600.00"))
        self.assertEqual(summary["retirement_deductions"], Decimal("2400.00"))  # reconstructed gross 12000 - 9600

    def test_retirement_vehicle_controls_payout_deductions_and_contribution_cost(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
            pension_tax_rate=Decimal("20.00"),
            health_insurance_rate=Decimal("10.00"),
            church_tax_rate=Decimal("0.00"),
            solidarity_surcharge_rate=Decimal("0.00"),
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        RetirementPlan.objects.create(
            household=household,
            person=person,
            name="Ruerup",
            vehicle_type=RetirementPlan.VehicleType.RUERUP,
            current_pension_points=Decimal("0.000"),
            expected_annual_points=Decimal("0.000"),
            pension_value_per_point=Decimal("100.00"),
            private_monthly_pension=Decimal("1000.00"),
            retirement_start_month=date(2026, 3, 1),
            annual_adjustment_rate=Decimal("0.00"),
            monthly_contribution=Decimal("200.00"),
            contribution_start_month=date(2026, 1, 1),
            contribution_relief_rate=Decimal("25.00"),
            payout_taxable_rate=Decimal("50.00"),
            payout_health_insurance_rate=Decimal("0.00"),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].retirement_income, Decimal("0.00"))
        self.assertEqual(projection[0].transfers, Decimal("150.00"))
        self.assertEqual(projection[0].liquid_balance, Decimal("850.00"))
        self.assertEqual(projection[1].transfers, Decimal("150.00"))
        self.assertEqual(projection[2].transfers, Decimal("0.00"))
        # 1,000 gross with only 50% exposed to 20% pension tax and no health
        # insurance exposure nets to 900.
        self.assertEqual(projection[2].retirement_income, Decimal("900.00"))
        self.assertEqual(projection[2].liquid_balance, Decimal("1600.00"))
        self.assertIn("Retirement contribution", {line.section for line in projection[0].audit_lines})

    def test_quarterly_equity_grant_adds_net_income_on_vest_months(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 6, 1),
            planning_months=7,
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        EquityGrant.objects.create(
            household=household,
            person=person,
            name="RSU",
            grant_type=EquityGrant.GrantType.RSU,
            gross_vest_value=Decimal("1000.00"),
            withholding_rate=Decimal("45.00"),
            cadence=EquityGrant.Cadence.QUARTERLY,
            first_vest_month=date(2026, 6, 1),
            last_vest_month=date(2026, 12, 1),
        )

        projection = build_projection(household)

        self.assertEqual(projection[0].equity_income, Decimal("550.00"))
        self.assertEqual(projection[1].equity_income, Decimal("0.00"))
        self.assertEqual(projection[2].equity_income, Decimal("0.00"))
        self.assertEqual(projection[3].equity_income, Decimal("550.00"))
        self.assertEqual(projection[6].equity_income, Decimal("550.00"))
        self.assertEqual(projection[6].liquid_balance, Decimal("2650.00"))

    def test_future_cash_flows_route_through_configured_accounts(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=1,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        giro = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("1000.00"),
        )
        savings = AssetAccount.objects.create(
            household=household,
            name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            balance=Decimal("500.00"),
        )
        household.default_operating_account = giro
        household.save(update_fields=["default_operating_account"])
        SalaryChange.objects.create(
            person=adult,
            name="Raise",
            start_month=date(2026, 1, 1),
            monthly_net_income_delta=Decimal("200.00"),
            account=savings,
        )
        EquityGrant.objects.create(
            household=household,
            person=adult,
            name="RSU",
            grant_type=EquityGrant.GrantType.RSU,
            gross_vest_value=Decimal("1000.00"),
            withholding_rate=Decimal("45.00"),
            cadence=EquityGrant.Cadence.QUARTERLY,
            first_vest_month=date(2026, 1, 1),
            last_vest_month=date(2026, 1, 1),
        )
        TrueExpense.objects.create(
            household=household,
            name="Insurance",
            amount=Decimal("300.00"),
            cadence=TrueExpense.Cadence.ONCE,
            first_due_month=date(2026, 1, 1),
            account=savings,
        )

        projection = build_projection(household)
        first_month = projection[0]
        lines = {line.name: line for line in first_month.audit_lines}

        self.assertEqual(first_month.liquid_balance, Decimal("1950.00"))
        self.assertEqual(first_month.account_balances[giro.id], Decimal("1550.00"))
        self.assertEqual(first_month.account_balances[savings.id], Decimal("400.00"))
        self.assertIn("to Tagesgeld", lines["Raise"].note)
        self.assertIn("to Giro", lines["RSU"].note)
        self.assertIn("from Tagesgeld", lines["Insurance"].note)

    def test_liquidity_view_flags_cash_stress_with_positive_net_worth(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=1,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("100.00"),
        )
        AssetAccount.objects.create(
            household=household,
            name="Depot",
            account_type=AssetAccount.AccountType.DEPOT,
            balance=Decimal("5000.00"),
        )
        MoneyRule.objects.create(
            household=household,
            name="Repair",
            kind=MoneyRule.Kind.EXPENSE,
            amount=Decimal("500.00"),
        )

        projection = build_projection(household)
        liquidity_view = build_liquidity_view(projection)

        self.assertEqual(len(liquidity_view["stress_months"]), 1)
        self.assertEqual(liquidity_view["lowest_liquid_month"]["liquid_balance"], Decimal("-400.00"))
        self.assertEqual(liquidity_view["lowest_net_worth_month"]["net_worth"], Decimal("4600.00"))

    def test_true_expense_salary_change_child_milestone_and_scenario_affect_projection(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("1000.00"),
            start_month=date(2026, 1, 1),
            planning_months=3,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        child = Person.objects.create(household=household, name="Mia", role=Person.Role.CHILD)
        TrueExpense.objects.create(
            household=household,
            name="Insurance",
            amount=Decimal("300.00"),
            cadence=TrueExpense.Cadence.ONCE,
            first_due_month=date(2026, 2, 1),
        )
        SalaryChange.objects.create(
            person=adult,
            name="Raise",
            start_month=date(2026, 2, 1),
            monthly_net_income_delta=Decimal("200.00"),
        )
        ChildMilestone.objects.create(
            person=child,
            name="School",
            start_month=date(2026, 2, 1),
            monthly_cost_delta=Decimal("50.00"),
            monthly_income_delta=Decimal("10.00"),
        )
        scenario = Scenario.objects.create(
            household=household,
            name="Stress",
            liquid_balance_delta=Decimal("100.00"),
            monthly_income_delta=Decimal("20.00"),
            monthly_expense_delta=Decimal("30.00"),
        )

        projection = build_projection(household, scenario=scenario)

        self.assertEqual(projection[0].liquid_balance, Decimal("1090.00"))
        self.assertEqual(projection[1].true_expenses, Decimal("300.00"))
        self.assertEqual(projection[1].salary_change_income, Decimal("200.00"))
        self.assertEqual(projection[1].child_income, Decimal("10.00"))
        self.assertEqual(projection[1].child_expenses, Decimal("50.00"))
        self.assertEqual(projection[1].scenario_income, Decimal("20.00"))
        self.assertEqual(projection[1].scenario_expenses, Decimal("30.00"))
        self.assertEqual(projection[1].liquid_balance, Decimal("940.00"))

    def test_future_salary_change_counts_in_long_range_projection(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            planning_years=5,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        SalaryChange.objects.create(
            person=adult,
            name="Future raise",
            start_month=date(2029, 1, 1),
            monthly_net_income_delta=Decimal("500.00"),
        )

        projection = build_projection(household)

        self.assertEqual(len(projection), 60)
        self.assertEqual(projection[35].salary_change_income, Decimal("0.00"))
        self.assertEqual(projection[36].salary_change_income, Decimal("500.00"))

    def test_plan_page_explains_and_lists_salary_changes(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            planning_years=5,
        )
        adult = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        MoneyRule.objects.create(
            household=household,
            person=adult,
            name="Current salary",
            kind=MoneyRule.Kind.INCOME,
            amount=Decimal("3200.00"),
            cadence=MoneyRule.Cadence.MONTHLY,
            start_month=date(2026, 1, 1),
        )
        SalaryChange.objects.create(
            person=adult,
            name="Promotion raise",
            start_month=date(2029, 1, 1),
            monthly_net_income_delta=Decimal("500.00"),
        )

        response = self.client.get(reverse("planner:plan_index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rules are the baseline")
        self.assertContains(response, "Salary changes are dated")
        self.assertContains(response, "For 3,200 to 3,700, enter +500")
        self.assertContains(response, "RSUs create cash first")
        self.assertContains(response, "Add RSU / equity grant")
        self.assertContains(response, "Add transfer rule")
        self.assertContains(response, "Promotion raise")
        self.assertContains(response, "Salary change")
        self.assertContains(response, "2029-01")
        self.assertContains(response, "<h2>Income Rules</h2>", html=True)
        self.assertContains(response, "<h2>Expense Rules</h2>", html=True)

    def test_salary_change_form_explains_delta_amount(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)

        response = self.client.get(reverse("planner:salary_change_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Salary changes are deltas against the current recurring salary rule")
        self.assertContains(response, "3,200 -&gt; 3,700 means +500")
        self.assertContains(response, "Use a negative value for a reduction")

    def test_equity_grant_form_explains_rsu_cash_and_etf_transfer(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)

        response = self.client.get(reverse("planner:equity_grant_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add RSU / equity grant")
        self.assertContains(response, "RSUs and other equity grants add net cash to the forecast")
        self.assertContains(response, "add a separate expense/transfer rule with the depot account as target")
        self.assertContains(response, "Gross value per vesting event")


class SeedDemoCommandTests(TestCase):
    def test_create_demo_user_requires_explicit_allow_in_production_mode(self):
        with patch.dict(
            os.environ,
            {"LIF_DEMO_PASSWORD": "long-enough-demo-password"},
            clear=True,
        ), override_settings(DEBUG=False):
            with self.assertRaises(CommandError):
                call_command("create_demo_user")

    def test_create_demo_user_creates_and_updates_normal_login(self):
        out = StringIO()

        with patch.dict(
            os.environ,
            {"LIF_ALLOW_DEMO_USER": "1", "LIF_DEMO_PASSWORD": "long-enough-demo-password"},
        ), override_settings(DEBUG=False):
            call_command("create_demo_user", stdout=out)

        with patch.dict(
            os.environ,
            {"LIF_ALLOW_DEMO_USER": "1", "LIF_DEMO_PASSWORD": "changed-demo-password"},
        ), override_settings(DEBUG=False):
            call_command("create_demo_user", stdout=out)

        User = get_user_model()
        user = User.objects.get(username="demo")

        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password("changed-demo-password"))
        self.assertIn("Created demo user 'demo'.", out.getvalue())
        self.assertIn("Updated demo user 'demo'.", out.getvalue())

    def test_seed_demo_creates_refreshable_demo_household(self):
        call_command("seed_demo")
        call_command("seed_demo")

        household = Household.objects.get(name=HOUSEHOLD_NAME)

        self.assertEqual(Household.objects.filter(name=HOUSEHOLD_NAME).count(), 1)
        self.assertEqual(household.data_mode, Household.DataMode.DEMO)
        self.assertEqual(household.planning_years, 40)
        self.assertEqual(household.projection_months, 480)
        self.assertEqual(household.resolved_display_granularity, Household.DisplayGranularity.YEARLY)
        self.assertEqual(household.pension_tax_rate, Decimal("18.00"))
        self.assertEqual(household.default_income_growth_rate, Decimal("2.00"))
        self.assertEqual(household.capital_gains_tax_rate, Decimal("25.00"))
        self.assertEqual(household.vorabpauschale_basiszins_rate, Decimal("3.20"))
        self.assertEqual(household.health_insurance_rate, Decimal("11.00"))
        self.assertEqual(household.emergency_fund_months, Decimal("6.00"))
        self.assertEqual(household.people.count(), 4)
        self.assertEqual(household.people.filter(role=Person.Role.ADULT).count(), 2)
        self.assertEqual(household.people.filter(role=Person.Role.CHILD).count(), 2)

        self.assertEqual(household.accounts.count(), 8)
        self.assertEqual(household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT).count(), 2)
        self.assertEqual(household.accounts.filter(account_type=AssetAccount.AccountType.LOAN).count(), 3)
        child_depot = household.accounts.get(name="Lina Kinderdepot")
        self.assertEqual(child_depot.owner_person.name, "Lina")
        self.assertFalse(child_depot.counts_in_household_net_worth)
        self.assertEqual(child_depot.effective_balance, Decimal("2500.00"))
        savings = household.accounts.get(account_type=AssetAccount.AccountType.SAVINGS)
        self.assertEqual(savings.savings_annual_interest_rate, Decimal("2.50"))
        self.assertEqual(savings.savings_interest_tax_rate, Decimal("25.00"))
        self.assertEqual(household.debts.count(), 3)
        self.assertEqual(
            set(household.debts.values_list("annual_interest_rate", flat=True)),
            {Decimal("2.85"), Decimal("4.10"), Decimal("3.20")},
        )
        self.assertEqual(
            set(household.debts.values_list("refinance_annual_interest_rate", flat=True)),
            {Decimal("4.25"), Decimal("4.75"), None},
        )
        self.assertEqual(
            set(household.debts.values_list("fixed_interest_until", flat=True)),
            {date(2036, 5, 1), date(2031, 5, 1), None},
        )
        self.assertEqual(household.income_investments.count(), 1)
        self.assertEqual(household.private_loans.count(), 2)
        self.assertEqual(household.properties.count(), 2)
        self.assertEqual(household.real_estate_transfer_plans.count(), 1)
        property_transfer = household.real_estate_transfer_plans.get(name="Gift residence to Lina with Nießbrauch")
        self.assertEqual(property_transfer.property_item.name, "Owner-occupied apartment")
        self.assertEqual(property_transfer.recipient.name, "Lina")
        self.assertTrue(property_transfer.retained_niessbrauch)
        self.assertEqual(property_transfer.taxable_gift_value, Decimal("300000.00"))
        self.assertEqual(household.cash_goals.count(), 2)
        self.assertEqual(household.retirement_plans.count(), 3)
        self.assertEqual(household.equity_grants.count(), 1)
        self.assertEqual(household.scenarios.count(), 2)
        self.assertEqual(household.true_expenses.count(), 4)
        self.assertEqual(household.snapshots.count(), 2)
        self.assertEqual(household.snapshot_reviews.count(), 1)
        self.assertEqual(household.assumption_reviews.count(), 2)
        self.assertEqual(household.import_batches.count(), 2)
        self.assertEqual(household.moneymoney_account_mappings.count(), 3)
        self.assertEqual(BackupEvent.objects.count(), 2)
        self.assertEqual(ChildMilestone.objects.filter(person__household=household).count(), 2)
        self.assertEqual(SalaryChange.objects.filter(person__household=household).count(), 2)
        self.assertTrue(feature_enabled("snapshots"))
        self.assertTrue(feature_enabled("moneymoney_import"))
        self.assertEqual(
            set(household.import_batches.values_list("source", flat=True)),
            {ImportBatch.Source.MONEYMONEY, ImportBatch.Source.CSV_DEPOT_HOLDINGS},
        )
        self.assertTrue(
            household.moneymoney_account_mappings.filter(
                account_name="MoneyMoney ETF Depot",
                account_type=AssetAccount.AccountType.DEPOT,
            ).exists()
        )
        snapshots = list(household.snapshots.order_by("snapshot_date"))
        self.assertEqual([snapshot.name for snapshot in snapshots], ["Annual review baseline 2025", "Annual review result 2025"])
        self.assertEqual(snapshots[0].summary["totals"]["net_worth"], "215000.00")
        self.assertEqual(snapshots[1].summary["totals"]["net_worth"], "268500.00")
        self.assertEqual(snapshots[1].summary["counts"]["holdings"], 7)
        saved_review = household.snapshot_reviews.get()
        self.assertEqual(saved_review.title, "2025 annual review")
        self.assertEqual(saved_review.baseline_snapshot, snapshots[0])
        self.assertEqual(saved_review.comparison_snapshot, snapshots[1])
        self.assertEqual(saved_review.actions.count(), 2)
        self.assertEqual(saved_review.actions.filter(status=SnapshotReviewAction.Status.OPEN).count(), 1)
        assumption_reviews = {review.key: review for review in household.assumption_reviews.all()}
        self.assertEqual(assumption_reviews["household:inflation"].reviewed_by, "Alex")
        self.assertLess(
            (timezone.now() - assumption_reviews["household:inflation"].reviewed_at).days,
            ASSUMPTION_REVIEW_EXPIRY_DAYS,
        )
        self.assertEqual(assumption_reviews["tax:capital-gains-tax"].reviewed_by, "Sam")
        self.assertGreater(
            (timezone.now() - assumption_reviews["tax:capital-gains-tax"].reviewed_at).days,
            ASSUMPTION_REVIEW_EXPIRY_DAYS,
        )
        grant = household.equity_grants.get()
        self.assertEqual(grant.gross_vest_value, Decimal("4200.00"))
        self.assertEqual(grant.net_vest_value, Decimal("2226.0000"))
        self.assertEqual(grant.first_vest_month, date(2026, 9, 1))
        self.assertEqual(grant.last_vest_month, date(2028, 6, 1))
        self.assertEqual(
            set(household.retirement_plans.values_list("retirement_start_month", flat=True)),
            {date(2053, 5, 1), date(2052, 10, 1)},
        )
        self.assertTrue(
            household.retirement_plans.filter(
                name="Alex bAV Direktversicherung",
                vehicle_type=RetirementPlan.VehicleType.BAV,
                monthly_contribution=Decimal("250.00"),
            ).exists()
        )
        solar = household.income_investments.get(investment_type=IncomeInvestment.InvestmentType.SOLAR)
        self.assertEqual(solar.monthly_income, Decimal("145.00"))
        self.assertEqual(solar.annual_growth_rate, Decimal("1.00"))
        self.assertEqual(solar.start_month, date(2027, 1, 1))
        self.assertEqual(solar.end_month, date(2028, 12, 1))
        residence = household.properties.get(use=RealEstate.Use.RESIDENCE)
        self.assertEqual(residence.saved_monthly_rent, Decimal("1600.00"))
        self.assertEqual(residence.debts.count(), 2)
        rental = household.properties.get(use=RealEstate.Use.INVESTMENT)
        self.assertEqual(rental.monthly_rent, Decimal("780.00"))
        self.assertEqual(rental.vacancy_rate, Decimal("5.00"))
        self.assertEqual(rental.rent_tax_rate, Decimal("30.00"))
        self.assertEqual(rental.sale_month, date(2033, 6, 1))
        self.assertEqual(rental.debts.count(), 1)
        self.assertEqual(rental.debts.get().current_principal, Decimal("95000.00"))
        self.assertEqual(DepotHolding.objects.filter(asset_account__household=household).count(), 8)
        self.assertEqual(
            DepotHolding.objects.filter(asset_account__household=household, asset_class__icontains="distributing").count(),
            3,
        )
        self.assertEqual(
            DepotHolding.objects.filter(asset_account__household=household, asset_class__icontains="Bond").count(),
            2,
        )
        self.assertEqual(
            DepotHolding.objects.filter(asset_account__household=household, asset_class__icontains="Stock").count(),
            2,
        )
        target_maturity_bond = DepotHolding.objects.get(asset_account__household=household, isin="IE0008UEVOE0")
        self.assertEqual(target_maturity_bond.payout_date, date(2028, 12, 31))
        self.assertEqual(target_maturity_bond.payout_amount, Decimal("1100.00"))
        depot = household.accounts.get(name="ETF Depot")
        self.assertEqual(depot.depot_valuation, AssetAccount.DepotValuation.HOLDINGS_SUM)
        self.assertEqual(depot.depot_annual_distribution_rate, Decimal("0.00"))
        self.assertEqual(depot.depot_teilfreistellung_rate, Decimal("30.00"))
        self.assertTrue(depot.depot_vorabpauschale_enabled)
        holding_value = sum((holding.current_value for holding in depot.holdings.all()), Decimal("0.00"))
        self.assertEqual(holding_value, Decimal("50000.00000000"))
        vwrl = DepotHolding.objects.get(asset_account__household=household, isin="IE00B3RBWM25")
        self.assertEqual(vwrl.annual_distribution_rate, Decimal("1.80"))
        self.assertEqual(vwrl.distribution_cadence, AssetAccount.InterestCadence.QUARTERLY)
        world_acc = DepotHolding.objects.get(asset_account__household=household, isin="IE00B4L5Y983")
        self.assertEqual(world_acc.annual_distribution_rate, Decimal("0.00"))
        allianz = DepotHolding.objects.get(asset_account__household=household, isin="DE0008404005")
        self.assertEqual(allianz.annual_distribution_rate, Decimal("4.00"))
        self.assertEqual(allianz.distribution_cadence, AssetAccount.InterestCadence.YEARLY)
        self.assertEqual(household.family_gift_plans.count(), 2)
        first_gift = household.family_gift_plans.get(name="Lina Kinderdepot first gift")
        self.assertEqual(first_gift.giver.name, "Alex")
        self.assertEqual(first_gift.recipient.name, "Lina")
        self.assertEqual(first_gift.target_account, child_depot)
        self.assertEqual(first_gift.amount, Decimal("10000.00"))
        self.assertEqual(household.rules.filter(kind=MoneyRule.Kind.INCOME).count(), 5)
        self.assertFalse(household.rules.filter(name="ETF and stock distributions, net averaged").exists())
        annual_bonus = household.rules.get(name="Alex annual bonus")
        self.assertEqual(annual_bonus.cadence, MoneyRule.Cadence.YEARLY)
        self.assertEqual(annual_bonus.amount, Decimal("20000.00"))
        self.assertEqual(annual_bonus.start_month, date(2026, 12, 1))
        future_loan = household.private_loans.get(name="Future family startup loan")
        self.assertEqual(future_loan.current_principal, Decimal("10000.00"))
        self.assertEqual(future_loan.disbursement_month, date(2027, 4, 1))
        self.assertEqual(future_loan.end_month, date(2030, 4, 1))
        self.assertEqual(future_loan.monthly_principal_repayment, Decimal("0.00"))
        self.assertEqual(household.transfer_rules.count(), 3)
        self.assertTrue(household.transfer_rules.filter(name="ETF savings plan", target_account=depot).exists())
        self.assertTrue(
            household.transfer_rules.filter(name="One-time ETF top-up", cadence=TransferRule.Cadence.ONCE).exists()
        )
        self.assertEqual(household.planned_investment_purchases.count(), 1)
        planned_bond = household.planned_investment_purchases.get(name="Planned 2027 bond ladder buy")
        self.assertEqual(planned_bond.purchase_month, date(2027, 12, 1))
        self.assertEqual(planned_bond.purchase_amount, Decimal("28450.00"))
        self.assertEqual(planned_bond.payout_date, date(2029, 1, 1))
        self.assertEqual(planned_bond.payout_amount, Decimal("30000.00"))
        self.assertGreaterEqual(household.rules.filter(kind=MoneyRule.Kind.EXPENSE).count(), 10)

        fire_household = Household.objects.get(name=FIRE_HOUSEHOLD_NAME)
        self.assertEqual(fire_household.data_mode, Household.DataMode.DEMO)
        self.assertTrue(fire_household.fund_cash_goal_from_depot)
        self.assertEqual(fire_household.people.count(), 1)
        self.assertEqual(fire_household.accounts.count(), 2)
        self.assertIsNotNone(fire_household.default_operating_account_id)
        self.assertEqual(fire_household.rules.filter(kind=MoneyRule.Kind.EXPENSE).count(), 0)
        self.assertEqual(fire_household.true_expenses.count(), 0)
        fire_depot = fire_household.accounts.get(account_type=AssetAccount.AccountType.DEPOT)
        self.assertEqual(fire_depot.balance, Decimal("750000.00"))
        fire_goal = fire_household.cash_goals.get()
        self.assertEqual(fire_goal.annual_amount, Decimal("28000.00"))
        self.assertNotEqual(fire_household.pk, household.pk)

        # The withdrawal rate is deliberately sustainable so the demo tells a
        # coherent FIRE story: the depot funds every month of spending across
        # the full horizon without depleting.
        fire_projection = build_projection(fire_household)
        self.assertTrue(all(month.invested_balance > 0 for month in fire_projection))
        self.assertTrue(all(month.liquid_balance > Decimal("-1.00") for month in fire_projection))
        self.assertTrue(any(line.section == "Depot draw" for month in fire_projection for line in month.audit_lines))

        response = self.client.get(reverse("planner:snapshot_review"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Annual Review")
        self.assertContains(response, "53500.00")
        self.assertContains(response, "2025 annual review")
        self.assertContains(response, "Review mortgage refinance assumptions")

        response = self.client.get(reverse("planner:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review mortgage refinance assumptions")
        self.assertNotContains(response, "Increase ETF savings plan after cash buffer review")

    def test_reset_demo_data_recreates_demo_without_deleting_real_households(self):
        real = Household.objects.create(
            name="Real",
            data_mode=Household.DataMode.REAL,
            starting_balance=Decimal("1234.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
            is_active=True,
        )
        call_command("seed_demo")

        call_command("reset_demo_data")

        real.refresh_from_db()
        demo = Household.objects.get(name=HOUSEHOLD_NAME)
        self.assertEqual(real.data_mode, Household.DataMode.REAL)
        self.assertEqual(real.starting_balance, Decimal("1234.00"))
        self.assertEqual(demo.data_mode, Household.DataMode.DEMO)
        self.assertEqual(Household.objects.filter(data_mode=Household.DataMode.DEMO).count(), 2)

    def test_seed_demo_if_needed_ignores_stale_marker_when_demo_missing(self):
        with TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / ".demo_seeded"
            marker.touch()

            call_command("seed_demo_if_needed", marker_file=str(marker))

        self.assertEqual(Household.objects.filter(data_mode=Household.DataMode.DEMO).count(), 2)
        self.assertTrue(Household.objects.filter(name=HOUSEHOLD_NAME).exists())

    def test_seed_demo_if_needed_does_not_recreate_existing_demo(self):
        call_command("seed_demo")
        household_id = Household.objects.get(name=HOUSEHOLD_NAME).pk

        with TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / ".demo_seeded"
            call_command("seed_demo_if_needed", marker_file=str(marker))
            self.assertTrue(marker.exists())

        self.assertEqual(Household.objects.filter(data_mode=Household.DataMode.DEMO).count(), 2)
        self.assertEqual(Household.objects.get(name=HOUSEHOLD_NAME).pk, household_id)


class FakeMoneyMoneyAccount:
    def __init__(self, name, balance, currency, positions=None):
        self.name = name
        self.balance = balance
        self.currency = currency
        self._positions = positions or []

    def positions(self):
        return self._positions


class FakeMoneyMoneyPosition:
    def __init__(self, name, isin, position_type, quantity, price, currency):
        self.name = name
        self.isin = isin
        self.type = position_type
        self.quantity = quantity
        self.price = price
        self.currencyOfPrice = currency
        self.currencyOfAmount = currency


class FakeMoneyMoneyClient:
    def __init__(self):
        self._accounts = [
            FakeMoneyMoneyAccount("Giro", "1234.567", "EUR"),
            FakeMoneyMoneyAccount("Tagesgeld", "5000", "EUR"),
        ]
        self._portfolios = [
            FakeMoneyMoneyAccount(
                "ING Depot",
                "50250.129",
                "EUR",
                positions=[
                    FakeMoneyMoneyPosition(
                        "Vanguard FTSE All-World",
                        "IE00B3RBWM25",
                        "ETF",
                        "120.1234567",
                        "118.425",
                        "EUR",
                    )
                ],
            )
        ]

    def accounts(self):
        return self._accounts

    def portfolios(self):
        return self._portfolios


class MoneyMoneyAdapterTests(TestCase):
    def test_decimal_string_normalizes_money_values(self):
        self.assertEqual(decimal_string("123.456"), "123.46")
        self.assertEqual(decimal_string("120.1234567", places="0.000001"), "120.123457")
        self.assertEqual(decimal_string(None), "0.00")

    def test_connector_maps_accounts_and_portfolios_to_import_rows(self):
        connector = MoneyMoneyConnector(client=FakeMoneyMoneyClient())

        rows = [row.as_csv_row() for row in connector.account_rows(as_of_date=date(2026, 6, 25))]

        self.assertEqual(
            rows,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "1234.57",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                    "source_key": "account:index:0:Giro",
                    "source_kind": "account",
                },
                {
                    "name": "Tagesgeld",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "5000.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                    "source_key": "account:index:1:Tagesgeld",
                    "source_kind": "account",
                },
                {
                    "name": "ING Depot",
                    "account_type": AssetAccount.AccountType.DEPOT,
                    "balance": "50250.13",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                    "source_key": "portfolio:index:0:ING Depot",
                    "source_kind": "portfolio",
                },
            ],
        )

    def test_connector_maps_portfolio_positions_to_holding_rows(self):
        connector = MoneyMoneyConnector(client=FakeMoneyMoneyClient())

        rows = [row.as_csv_row() for row in connector.depot_holding_rows(as_of_date=date(2026, 6, 25))]

        self.assertEqual(
            rows,
            [
                {
                    "account_name": "ING Depot",
                    "name": "Vanguard FTSE All-World",
                    "isin": "IE00B3RBWM25",
                    "ticker": "",
                    "asset_class": "ETF",
                    "quantity": "120.123457",
                    "latest_price": "118.42",
                    "currency": "EUR",
                    "as_of_date": "2026-06-25",
                    "payout_date": "",
                    "payout_amount": "",
                    "account_source_key": "portfolio:index:0:ING Depot",
                }
            ],
        )

    def test_legacy_override_applies_through_name_fallback(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        # Migrated/pre-preview override keyed by name only.
        MoneyMoneyAccountMapping.objects.create(
            household=household,
            account_name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
        )
        overrides = moneymoney_account_type_overrides(household)
        connector = MoneyMoneyConnector(client=FakeMoneyMoneyClient())

        rows = {row.name: row for row in connector.account_rows(account_type_overrides=overrides)}

        # The discovered source key (account:index:1:Tagesgeld) never matches the
        # legacy-name key, but the override still applies via the name fallback.
        self.assertEqual(rows["Tagesgeld"].account_type, AssetAccount.AccountType.SAVINGS)
        self.assertEqual(rows["Giro"].account_type, AssetAccount.AccountType.CASH)

    def test_discovery_migrates_legacy_mapping_to_real_source_key(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        legacy = MoneyMoneyAccountMapping.objects.create(
            household=household,
            account_name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
            import_enabled=False,
            notes="keep me",
        )
        self.assertEqual(legacy.source_key, "legacy-name:Tagesgeld")

        sync_moneymoney_mapping_rows(
            household,
            [
                ImportedAccountRow(
                    name="Tagesgeld",
                    account_type=AssetAccount.AccountType.CASH,
                    balance="5000.00",
                    currency="EUR",
                    source_key="account:index:1:Tagesgeld",
                    source_kind="account",
                )
            ],
        )

        self.assertFalse(
            MoneyMoneyAccountMapping.objects.filter(household=household, source_key="legacy-name:Tagesgeld").exists()
        )
        migrated = MoneyMoneyAccountMapping.objects.get(household=household, source_key="account:index:1:Tagesgeld")
        self.assertEqual(migrated.account_type, AssetAccount.AccountType.SAVINGS)
        self.assertFalse(migrated.import_enabled)
        self.assertEqual(migrated.notes, "keep me")

    def test_mapping_review_merges_legacy_mapping_into_preview_row(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        MoneyMoneyAccountMapping.objects.create(
            household=household,
            account_name="Tagesgeld",
            account_type=AssetAccount.AccountType.SAVINGS,
        )
        ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.MONEYMONEY,
            status=ImportBatch.Status.DRY_RUN,
            filename="moneymoney_accounts",
            row_count=1,
            valid_count=1,
            error_count=0,
            summary={
                "import_kind": "moneymoney_accounts",
                "rows": [
                    {
                        "values": {
                            "name": "Tagesgeld",
                            "source_key": "account:index:1:Tagesgeld",
                            "source_kind": "account",
                            "account_type": "cash",
                            "balance": "5000.00",
                            "currency": "EUR",
                        }
                    }
                ],
            },
        )

        review = build_moneymoney_mapping_review(household)
        tagesgeld_rows = [row for row in review["rows"] if row["account_name"] == "Tagesgeld"]

        # One row, not two; the legacy override is shown under the real key.
        self.assertEqual(len(tagesgeld_rows), 1)
        self.assertEqual(tagesgeld_rows[0]["source_key"], "account:index:1:Tagesgeld")
        self.assertEqual(tagesgeld_rows[0]["mapped_type"], AssetAccount.AccountType.SAVINGS)

    def test_csv_reimport_preserves_moneymoney_account_key(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("100.00"),
            currency="EUR",
            source=AssetAccount.Source.MONEYMONEY,
            moneymoney_account_key="account:7",
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "250.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                }
            ],
            start_row_number=1,
        )
        summary = dry_run_summary(result)
        batch = ImportBatch.objects.create(
            household=household,
            source=ImportBatch.Source.CSV_ACCOUNTS,
            status=ImportBatch.Status.DRY_RUN,
            filename="accounts.csv",
            row_count=result["row_count"],
            valid_count=result["valid_count"],
            error_count=result["error_count"],
            summary=summary,
        )

        with patch("planner.imports.call_command"):
            apply_account_import_batch(batch)

        account = household.accounts.get(name="Giro")
        self.assertEqual(account.moneymoney_account_key, "account:7")  # not wiped
        self.assertEqual(account.balance, Decimal("250.00"))

    def test_account_values_unchanged_ignores_blank_source_key(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        account = AssetAccount.objects.create(
            household=household,
            name="Giro",
            account_type=AssetAccount.AccountType.CASH,
            balance=Decimal("100.00"),
            currency="EUR",
            institution="",
            moneymoney_account_key="account:7",
        )
        values = {
            "account_type": AssetAccount.AccountType.CASH,
            "balance": "100.00",
            "currency": "EUR",
            "institution": "",
            "as_of_date": "",
            "source_key": "",  # CSV row carries no source key
        }
        self.assertTrue(account_values_unchanged(account, values))

    def test_duplicate_names_with_distinct_source_keys_do_not_warn(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=12,
        )
        result = account_rows_dry_run(
            household,
            [
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "100.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                    "source_key": "account:one",
                    "source_kind": "account",
                },
                {
                    "name": "Giro",
                    "account_type": AssetAccount.AccountType.CASH,
                    "balance": "200.00",
                    "currency": "EUR",
                    "institution": "",
                    "as_of_date": "2026-06-25",
                    "source_key": "account:two",
                    "source_kind": "account",
                },
            ],
            start_row_number=1,
        )

        warnings = [warning for row in result["rows"] for warning in row.warnings]
        self.assertEqual(warnings, [])

    def test_seed_demo_removes_untouched_default_household(self):
        Household.objects.create(
            name="Home",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 6, 1),
            planning_months=24,
        )

        call_command("seed_demo")

        self.assertFalse(Household.objects.filter(name="Home").exists())
        self.assertEqual(Household.objects.first().name, HOUSEHOLD_NAME)


class MCPServerTests(TestCase):
    def _enable_flag(self):
        FeatureFlag.objects.update_or_create(
            key="mcp_server",
            defaults={"enabled": True, "description": FEATURE_FLAG_DEFINITIONS["mcp_server"]["description"]},
        )

    def _household(self):
        household = Household.objects.create(
            name="Test",
            starting_balance=Decimal("0.00"),
            start_month=date(2026, 1, 1),
            planning_months=24,
            currency="EUR",
        )
        person = Person.objects.create(household=household, name="Alex", role=Person.Role.ADULT)
        AssetAccount.objects.create(
            household=household, name="Giro", account_type=AssetAccount.AccountType.CASH, balance=Decimal("1000.00")
        )
        depot = AssetAccount.objects.create(
            household=household, name="Depot", account_type=AssetAccount.AccountType.DEPOT, balance=Decimal("0.00")
        )
        mortgage = AssetAccount.objects.create(
            household=household, name="Mortgage", account_type=AssetAccount.AccountType.LOAN, balance=Decimal("100000.00")
        )
        Debt.objects.create(
            household=household, account=mortgage, name="Mortgage",
            current_principal=Decimal("100000.00"), annual_interest_rate=Decimal("3.00"),
            monthly_payment=Decimal("1000.00"), start_month=date(2026, 1, 1),
        )
        RetirementPlan.objects.create(
            household=household, person=person, name="Pension",
            current_pension_points=Decimal("10.000"), pension_value_per_point=Decimal("40.00"),
            retirement_start_month=date(2026, 1, 1),
        )
        PlannedInvestmentPurchase.objects.create(
            household=household,
            name="Future bond",
            asset_type=PlannedInvestmentPurchase.AssetType.BOND,
            target_account=depot,
            purchase_amount=Decimal("28450.00"),
            purchase_month=date(2027, 12, 1),
            payout_date=date(2029, 1, 1),
            payout_amount=Decimal("30000.00"),
        )
        CashGoal.objects.create(household=household, name="Need", annual_amount=Decimal("30000.00"), start_year=2026)
        return household

    def test_call_tool_blocked_when_flag_disabled(self):
        self._household()
        result = call_tool("overview")
        self.assertIn("disabled", result.get("error", "").lower())

    def test_call_tool_reports_missing_household(self):
        self._enable_flag()
        result = call_tool("overview")
        self.assertIn("No household", result.get("error", ""))

    def test_unknown_tool_returns_error(self):
        self._enable_flag()
        self._household()
        self.assertIn("Unknown tool", call_tool("does_not_exist").get("error", ""))

    def test_tool_definitions_cover_all_tools(self):
        names = {tool["name"] for tool in tool_definitions()}
        self.assertEqual(
            names,
            {"overview", "assumptions", "inputs", "projection", "audit_lines",
             "quality_report", "debt_schedules", "retirement_analysis", "income_timeline"},
        )
        for tool in tool_definitions():
            self.assertTrue(tool["description"])
            self.assertIn("type", tool["input_schema"])

    def test_each_tool_returns_json_serializable_payload(self):
        self._enable_flag()
        self._household()
        for tool in tool_definitions():
            payload = call_tool(tool["name"])
            self.assertNotIn("error", payload, f"{tool['name']} returned an error")
            # Must round-trip through JSON (Decimals/dates converted).
            json.dumps(payload)

    def test_overview_and_assumptions_expose_planning_knobs(self):
        self._enable_flag()
        self._household()
        overview = call_tool("overview")
        self.assertEqual(overview["assumptions"]["currency"], "EUR")
        self.assertIn("current_totals", overview)
        self.assertIn("quality_severity_counts", overview)
        assumptions = call_tool("assumptions")["assumptions"]
        self.assertEqual(assumptions["annual_inflation_rate"], "2.00")
        self.assertEqual(assumptions["default_income_growth_rate"], "0.00")
        self.assertEqual(assumptions["capital_income_allowance"], "2000.00")
        self.assertEqual(assumptions["vorabpauschale_basiszins_rate"], "3.20")
        self.assertEqual(assumptions["emergency_fund_months"], "0.00")
        self.assertEqual(assumptions["projection_months"], 24)

    def test_inputs_include_every_section(self):
        self._enable_flag()
        self._household()
        inputs = call_tool("inputs")
        self.assertEqual(inputs["accounts"][0]["name"] in {"Giro", "Depot", "Mortgage"}, True)
        self.assertEqual(len(inputs["debts"]), 1)
        self.assertEqual(inputs["debts"][0]["annual_interest_rate"], "3.00")
        self.assertEqual(len(inputs["retirement_plans"]), 1)
        self.assertEqual(len(inputs["cash_goals"]), 1)
        self.assertEqual(len(inputs["planned_investment_purchases"]), 1)
        self.assertEqual(inputs["planned_investment_purchases"][0]["purchase_month"], "2027-12-01")

    def test_projection_yearly_and_monthly(self):
        self._enable_flag()
        self._household()
        yearly = call_tool("projection")
        self.assertEqual(yearly["granularity"], "yearly")
        self.assertEqual(yearly["row_count"], 2)  # 24 months -> 2 years
        monthly = call_tool("projection", {"granularity": "monthly"})
        self.assertEqual(monthly["granularity"], "monthly")
        self.assertEqual(monthly["row_count"], 24)

    def test_debt_and_retirement_tools(self):
        self._enable_flag()
        self._household()
        debts = call_tool("debt_schedules")["debts"]
        self.assertEqual(debts[0]["name"], "Mortgage")
        self.assertIn("payoff_month", debts[0]["summary"])
        retirement = call_tool("retirement_analysis")["retirement_years"]
        self.assertTrue(retirement)
        self.assertIn("tax_summary", retirement[0])
