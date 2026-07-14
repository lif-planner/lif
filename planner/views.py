import sqlite3
import shutil
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib import messages
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.staticfiles import finders
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.html import format_html
from django.utils import timezone

from lif.version import version_context
from lif.views import migrations_current

from .forms import (
    AccountCsvImportForm,
    AccountSetupWizardForm,
    AssetAccountForm,
    CashGoalForm,
    ChildMilestoneForm,
    DebtForm,
    DepotHoldingCsvImportForm,
    DepotHoldingForm,
    EquityGrantForm,
    FamilyGiftPlanForm,
    FirstRunSetupForm,
    GoalPlannerForm,
    HouseholdForm,
    IncomeInvestmentForm,
    MoneyMoneyMappingAddForm,
    MoneyRuleForm,
    PersonForm,
    PlannedInvestmentPurchaseForm,
    PrivateLoanReceivableForm,
    RealEstateForm,
    RealEstateTransferPlanForm,
    RetirementPlanForm,
    SalaryChangeForm,
    ScenarioForm,
    ScenarioHouseholdCloneForm,
    SnapshotForm,
    SnapshotReviewActionForm,
    SnapshotReviewForm,
    TransferRuleForm,
    TrueExpenseForm,
)
from .feature_flags import FEATURE_FLAG_DEFINITIONS, environment_override, feature_flag_map, feature_required
from .import_adapters.moneymoney import MoneyMoneyConnector, MoneyMoneyConnectorUnavailable
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
    DEPOT_HOLDING_COLUMNS,
    DEPOT_HOLDING_OPTIONAL_COLUMNS,
    account_rows_dry_run,
    account_csv_dry_run,
    apply_account_import_batch,
    apply_depot_holding_import_batch,
    depot_holding_rows_dry_run,
    depot_holding_csv_dry_run,
    dry_run_summary,
)
from .liquidity import build_liquidity_view, build_yearly_liquidity_view
from .projections import (
    build_projection,
    build_yearly_projection,
    first_of_month,
    iter_debt_schedule,
    MAX_AMORTIZATION_MONTHS,
    rule_applies,
    summarize_debt,
)
from .projection_integrity import check_projection_integrity
from .quality import build_quality_report, build_retirement_health_issues, emergency_fund_target, recurring_monthly_expenses
from .retirement import retirement_tax_summary
from .sequence_risk import build_sequence_risk_summary
from .analytics import build_analytics_data, build_assumption_sensitivity, build_income_timeline, build_scenario_comparison
from .assumptions import HOUSEHOLD_ASSUMPTION_FIELDS, build_assumption_registry, build_assumption_review_center
from .finance import real_value
from .forecast_explain import (
    GROUPS,
    account_ledger_rows,
    account_ledger_years,
    cash_flow_ledger_rows,
    forecast_driver_summary,
    forecast_warnings,
    forecast_explanation_summary,
    general_pool_ledger_rows,
    general_pool_ledger_years,
    grouped_audit_lines,
)
from .goal_planner import solve_monthly_contribution
from .households import active_household
from .household_clone import clone_household
from .exports import (
    account_statement_rows,
    cash_flow_headers,
    cash_flow_rows,
    csv_response,
    general_pool_statement_rows,
    projection_month_headers,
    projection_month_rows,
    projection_year_headers,
    projection_year_rows,
    statement_headers,
)
from .readiness import (
    build_household_readiness,
    build_import_batch_detail,
    build_import_reconciliation,
    build_import_runbook,
    checklist_item,
    create_pre_import_snapshot,
    decorate_import_batch,
)
from .reports import build_report_year
from .moneymoney_service import (
    build_moneymoney_mapping_review,
    disabled_moneymoney_source_keys,
    moneymoney_account_type_overrides,
    run_moneymoney_diagnostics,
    sync_moneymoney_mapping_rows,
)
from .balance_sheet import current_balance_sheet
from .privacy import PRIVACY_MODE_SESSION_KEY
from .snapshots import (
    build_projection_change_drivers,
    build_snapshot_summary,
    compare_projection_summaries,
    compare_snapshot_summaries,
    compare_snapshot_to_current,
)


DEPOT_HOLDING_HINT = (
    "For distributing ETFs, enter the holding here and model expected net payouts separately for now, "
    "for example as a monthly averaged income rule."
)
SALARY_CHANGE_HINT = (
    "Salary changes are deltas against the current recurring salary rule. "
    "If salary changes from 3,200 to 3,700, enter +500. "
    "If salary drops from 3,700 to 3,200, enter -500. "
    "The delta starts in the selected month and remains active until the optional end month."
)
EQUITY_GRANT_HINT = (
    "RSUs and other equity grants add net cash to the forecast on vesting months. "
    "Gross vest value is per vesting event, not the total grant. "
    "To model investing the cash into an ETF, add a separate expense/transfer rule with the depot account as target."
)
INCOME_INVESTMENT_HINT = (
    "Use this for dated projects such as solar investments that produce recurring cash income. "
    "Principal is the invested or tied-up capital for yield context. "
    "Monthly income is the expected cash inflow per month; for irregular payments, enter a monthly average."
)
SETUP_SLOT_PREFIX = "Setup slot: "
SAFE_PORTFOLIO_DRAW_RATE = Decimal("4.00")


def get_household():
    return active_household(create=True)


def slot_note(slot):
    return f"{SETUP_SLOT_PREFIX}{slot}"


def setup_person(household, slot, name, role, birth_date):
    person = household.people.filter(notes=slot_note(slot)).first()
    if person is None:
        slot_indexes = {"adult_1": 0, "adult_2": 1, "child_1": 0, "child_2": 1}
        slot_index = slot_indexes.get(slot)
        existing_people = list(household.people.filter(role=role).order_by("created_at", "pk"))
        if slot_index is not None and slot_index < len(existing_people):
            person = existing_people[slot_index]
    if person is None:
        person = household.people.filter(name=name, role=role).order_by("created_at", "pk").first()
    if person is None:
        person = Person(household=household)
    person.name = name
    person.role = role
    person.birth_date = birth_date
    person.notes = slot_note(slot)
    person.save()
    return person


def setup_money_rule(household, key_name, display_name, person, amount, category):
    rule = household.rules.filter(name=key_name).first()
    if rule is None:
        rule = MoneyRule(household=household, name=key_name)
    rule.person = person
    rule.kind = MoneyRule.Kind.INCOME
    rule.amount = amount
    rule.cadence = MoneyRule.Cadence.MONTHLY
    rule.start_month = household.start_month
    rule.end_month = None
    rule.category = category
    rule.notes = display_name
    rule.is_active = True
    rule.save()
    return rule


def setup_initial_data(household, data):
    with transaction.atomic():
        household.name = data["household_name"]
        household.data_mode = Household.DataMode.REAL
        household.currency = data["currency"]
        household.start_month = data["start_month"]
        household.planning_years = data["planning_years"]
        household.planning_months = min(household.planning_months or 12, 60)
        household.display_granularity = Household.DisplayGranularity.AUTO
        household.save()

        adults = [
            setup_person(household, "adult_1", data["adult_1_name"], Person.Role.ADULT, data["adult_1_birth_date"]),
            setup_person(household, "adult_2", data["adult_2_name"], Person.Role.ADULT, data["adult_2_birth_date"]),
        ]
        children = [
            setup_person(household, "child_1", data["child_1_name"], Person.Role.CHILD, data["child_1_birth_date"]),
            setup_person(household, "child_2", data["child_2_name"], Person.Role.CHILD, data["child_2_birth_date"]),
        ]

        setup_money_rule(
            household,
            "Setup salary adult 1",
            f"Net salary {adults[0].name}",
            adults[0],
            data["adult_1_monthly_salary"],
            "Salary",
        )
        setup_money_rule(
            household,
            "Setup salary adult 2",
            f"Net salary {adults[1].name}",
            adults[1],
            data["adult_2_monthly_salary"],
            "Salary",
        )
        setup_money_rule(
            household,
            "Setup Kindergeld child 1",
            f"Kindergeld {children[0].name}",
            children[0],
            data["child_1_kindergeld"],
            "Benefits",
        )
        setup_money_rule(
            household,
            "Setup Kindergeld child 2",
            f"Kindergeld {children[1].name}",
            children[1],
            data["child_2_kindergeld"],
            "Benefits",
        )

        CashGoal.objects.update_or_create(
            household=household,
            name="Baseline FIRE cash need",
            defaults={
                "annual_amount": data["annual_cash_goal"],
                "indexed_to_inflation": True,
                "start_year": household.start_month.year,
                "end_year": None,
                "is_active": True,
                "notes": "Created from first-run setup.",
            },
        )


def setup_initial_values(household):
    today = timezone.localdate()
    cash_goal = household.cash_goals.filter(name="Baseline FIRE cash need").first()
    initial = {
        "household_name": household.name,
        "currency": household.currency,
        "start_month": household.start_month,
        "planning_years": household.planning_years or 40,
        "annual_cash_goal": cash_goal.annual_amount if cash_goal else Decimal("30000.00"),
        "adult_1_birth_date": date(today.year - 40, 1, 1),
        "adult_2_birth_date": date(today.year - 40, 7, 1),
        "child_1_birth_date": date(today.year - 10, 1, 1),
        "child_2_birth_date": date(today.year - 8, 1, 1),
    }
    for slot in ["adult_1", "adult_2", "child_1", "child_2"]:
        person = household.people.filter(notes=slot_note(slot)).first()
        if person:
            initial[f"{slot}_name"] = person.name
            initial[f"{slot}_birth_date"] = person.birth_date

    for field, rule_name in [
        ("adult_1_monthly_salary", "Setup salary adult 1"),
        ("adult_2_monthly_salary", "Setup salary adult 2"),
        ("child_1_kindergeld", "Setup Kindergeld child 1"),
        ("child_2_kindergeld", "Setup Kindergeld child 2"),
    ]:
        rule = household.rules.filter(name=rule_name).first()
        if rule:
            initial[field] = rule.amount
    return initial


def account_setup_initial(household):
    return {
        "currency": household.currency,
        "as_of_date": timezone.localdate(),
        "debt_start_month": household.start_month,
    }


def create_account_from_setup(household, data):
    with transaction.atomic():
        account = AssetAccount.objects.create(
            household=household,
            name=data["name"],
            account_type=data["account_type"],
            balance=data["balance"],
            currency=data["currency"],
            institution=data.get("institution", ""),
            as_of_date=data.get("as_of_date"),
            notes=data.get("notes", ""),
            depot_valuation=data.get("depot_valuation") or AssetAccount.DepotValuation.ACCOUNT_BALANCE,
            depot_annual_return_rate=data.get("depot_annual_return_rate") or Decimal("0.00"),
            depot_teilfreistellung_rate=data.get("depot_teilfreistellung_rate") or Decimal("30.00"),
            depot_vorabpauschale_enabled=bool(data.get("depot_vorabpauschale_enabled")),
            savings_annual_interest_rate=data.get("savings_annual_interest_rate") or Decimal("0.00"),
            savings_interest_cadence=data.get("savings_interest_cadence") or AssetAccount.InterestCadence.MONTHLY,
            savings_interest_tax_rate=data.get("savings_interest_tax_rate") or Decimal("25.00"),
        )

        holding = None
        if account.account_type == AssetAccount.AccountType.DEPOT and data.get("holding_name"):
            holding = DepotHolding.objects.create(
                asset_account=account,
                name=data["holding_name"],
                isin=data.get("holding_isin", ""),
                ticker=data.get("holding_ticker", ""),
                asset_class=data.get("holding_asset_class") or "ETF",
                quantity=data["holding_quantity"],
                latest_price=data["holding_latest_price"],
                currency=account.currency,
                as_of_date=account.as_of_date,
                payout_date=data.get("holding_payout_date"),
                payout_amount=data.get("holding_payout_amount"),
            )

        debt = None
        if account.account_type == AssetAccount.AccountType.LOAN:
            debt = Debt.objects.create(
                household=household,
                account=account,
                name=data.get("debt_name") or account.name,
                current_principal=abs(account.balance),
                annual_interest_rate=data["debt_annual_interest_rate"],
                monthly_payment=data["debt_monthly_payment"],
                start_month=data.get("debt_start_month"),
                end_month=data.get("debt_end_month"),
                fixed_interest_until=data.get("debt_fixed_interest_until"),
                refinance_annual_interest_rate=data.get("debt_refinance_annual_interest_rate"),
                refinance_monthly_payment=data.get("debt_refinance_monthly_payment"),
            )
        return account, holding, debt


def build_display_projection(household):
    projection = build_projection(household)
    yearly_projection = build_yearly_projection(projection, household.cash_goals.all())
    display_granularity = household.resolved_display_granularity
    if display_granularity == Household.DisplayGranularity.YEARLY:
        return projection, yearly_projection, yearly_projection, build_yearly_liquidity_view(yearly_projection)
    return projection, yearly_projection, projection, build_liquidity_view(projection)


def integrity_failure_url(failure, projection, yearly_projection):
    if failure["scope"] == "Year":
        for index, year in enumerate(yearly_projection):
            if year.label == failure["label"]:
                return reverse("planner:projection_year_audit", args=[index])
        return ""
    for month in projection:
        if month.month.strftime("%b %Y") == failure["label"]:
            return reverse("planner:projection_audit", args=[month.index])
    return ""


def projection_integrity_rows(projection, yearly_projection):
    first_month = projection[0] if projection else None
    last_month = projection[-1] if projection else None
    last_year = yearly_projection[-1] if yearly_projection else None
    if not first_month or not last_month:
        return []
    return [
        {
            "label": "Liquid balance",
            "opening": first_month.opening_liquid_balance,
            "ending": last_month.liquid_balance,
            "change": last_month.liquid_balance - first_month.opening_liquid_balance,
            "year_ending": last_year.ending_liquid_balance if last_year else last_month.liquid_balance,
            "detail": "Cash, savings, and any general liquid pool used by forecast rules.",
        },
        {
            "label": "Depot value",
            "opening": first_month.opening_invested_balance,
            "ending": last_month.invested_balance,
            "change": last_month.invested_balance - first_month.opening_invested_balance,
            "year_ending": last_year.ending_invested_balance if last_year else last_month.invested_balance,
            "detail": "Depot balances, holding growth, distributions, and planned buys/payouts.",
        },
        {
            "label": "Other assets",
            "opening": first_month.opening_other_asset_balance,
            "ending": last_month.other_asset_balance,
            "change": last_month.other_asset_balance - first_month.opening_other_asset_balance,
            "year_ending": last_year.ending_other_asset_balance if last_year else last_month.other_asset_balance,
            "detail": "Real estate, private-loan receivables, and other non-depot assets.",
        },
        {
            "label": "Liabilities",
            "opening": first_month.opening_liability_balance,
            "ending": last_month.liability_balance,
            "change": last_month.liability_balance - first_month.opening_liability_balance,
            "year_ending": last_year.ending_liability_balance if last_year else last_month.liability_balance,
            "detail": "Loan and mortgage balances after scheduled principal movements.",
        },
        {
            "label": "Net worth",
            "opening": first_month.opening_net_worth,
            "ending": last_month.net_worth,
            "change": last_month.net_worth - first_month.opening_net_worth,
            "year_ending": last_year.ending_net_worth if last_year else last_month.net_worth,
            "detail": "Liquid plus depot plus other assets minus liabilities.",
        },
    ]


def projection_integrity(request):
    household = get_household()
    projection = build_projection(household)
    yearly_projection = build_yearly_projection(projection, household.cash_goals.all())
    accounts = household.accounts.all()
    integrity = check_projection_integrity(projection, yearly_projection, accounts)
    failures = [
        {
            **failure,
            "url": integrity_failure_url(failure, projection, yearly_projection),
        }
        for failure in integrity["failures"]
    ]
    return render(
        request,
        "planner/projection_integrity.html",
        {
            "household": household,
            "integrity": {**integrity, "failures": failures},
            "bucket_rows": projection_integrity_rows(projection, yearly_projection),
            "first_month": projection[0] if projection else None,
            "last_month": projection[-1] if projection else None,
            "year_count": len(yearly_projection),
        },
    )


def account_reconciliation_rows(household):
    today = timezone.localdate()
    accounts = list(
        household.accounts.select_related("owner_person").prefetch_related("holdings").order_by("account_type", "name", "pk")
    )
    duplicate_names = {}
    for account in accounts:
        duplicate_names.setdefault(account.name.strip().lower(), 0)
        duplicate_names[account.name.strip().lower()] += 1

    rows = []
    for account in accounts:
        stale_days = (today - account.as_of_date).days if account.as_of_date else None
        related_counts = {
            "rules": household.rules.filter(account=account).count(),
            "transfers": household.transfer_rules.filter(Q(source_account=account) | Q(target_account=account)).count(),
            "debts": household.debts.filter(Q(account=account) | Q(source_account=account)).count(),
            "planned_purchases": household.planned_investment_purchases.filter(
                Q(source_account=account) | Q(target_account=account)
            ).count(),
            "family_gifts": household.family_gift_plans.filter(
                Q(source_account=account) | Q(target_account=account)
            ).count(),
            "income_investments": household.income_investments.filter(source_account=account).count(),
            "private_loans": household.private_loans.filter(source_account=account).count(),
            "properties": household.properties.filter(Q(source_account=account) | Q(sale_proceeds_account=account)).count(),
            "equity_grants": household.equity_grants.filter(account=account).count(),
        }
        related_total = sum(related_counts.values())
        issues = []
        if account.as_of_date is None:
            issues.append({"label": "No valuation date", "severity": "warning", "key": "missing_dates"})
        elif stale_days is not None and stale_days > 45:
            issues.append({"label": f"Stale by {stale_days} days", "severity": "warning", "key": "stale"})
        if account.account_type == AssetAccount.AccountType.DEPOT and account.holdings.exists() and account.depot_difference:
            issues.append(
                {"label": "Depot balance differs from holdings", "severity": "warning", "key": "depot_drift"}
            )
        if not account.counts_in_household_net_worth:
            issues.append({"label": "Tracked outside household net worth", "severity": "info", "key": "outside_net_worth"})
        if duplicate_names.get(account.name.strip().lower(), 0) > 1:
            issues.append({"label": "Duplicate display name", "severity": "info", "key": "duplicate_names"})
        if account.source == AssetAccount.Source.MONEYMONEY and not account.moneymoney_account_key:
            issues.append({"label": "MoneyMoney source key missing", "severity": "warning", "key": "moneymoney"})
        if account.account_type == AssetAccount.AccountType.DEPOT and not account.holdings.exists():
            issues.append({"label": "Depot has no holdings", "severity": "info", "key": "depot_drift"})
        warnings = [issue["label"] for issue in issues]
        issue_keys = []
        if account.as_of_date is None:
            issue_keys.append("missing_dates")
        if stale_days is not None and stale_days > 45:
            issue_keys.append("stale")
        if account.account_type == AssetAccount.AccountType.DEPOT and account.holdings.exists() and account.depot_difference:
            issue_keys.append("depot_drift")
        if not account.counts_in_household_net_worth:
            issue_keys.append("outside_net_worth")
        if duplicate_names.get(account.name.strip().lower(), 0) > 1:
            issue_keys.append("duplicate_names")
        if account.source == AssetAccount.Source.MONEYMONEY:
            issue_keys.append("moneymoney")
        if account.source == AssetAccount.Source.MANUAL:
            issue_keys.append("manual")
        actions = [{"label": "Edit account", "url": reverse("planner:account_update", args=[account.pk])}]
        if account.as_of_date is None or (stale_days is not None and stale_days > 45):
            actions.append({"label": "Refresh import", "url": reverse("planner:import_center")})
        if account.account_type == AssetAccount.AccountType.DEPOT:
            actions.append({"label": "Review holdings", "url": reverse("planner:holding_index")})
        if account.source == AssetAccount.Source.MONEYMONEY or account.moneymoney_account_key:
            actions.append({"label": "Review mappings", "url": reverse("planner:moneymoney_mappings")})

        rows.append(
            {
                "account": account,
                "issue_keys": issue_keys,
                "actions": actions,
                "stale_days": stale_days,
                "holdings_count": account.holdings.count(),
                "holdings_value": account.holdings_value,
                "depot_difference": account.depot_difference,
                "related_counts": related_counts,
                "related_total": related_total,
                "issues": issues,
                "warnings": warnings,
            }
        )
    return rows


def reconciliation_issue_groups(account_rows):
    severity_order = ["warning", "info"]
    labels = {"warning": "Review soon", "info": "Informational"}
    groups = []
    for severity in severity_order:
        issue_rows = []
        for row in account_rows:
            matching_issues = [issue for issue in row["issues"] if issue["severity"] == severity]
            if matching_issues:
                issue_rows.append({"account": row["account"], "issues": matching_issues, "actions": row["actions"]})
        if issue_rows:
            groups.append(
                {
                    "severity": severity,
                    "label": labels[severity],
                    "issue_count": sum(len(item["issues"]) for item in issue_rows),
                    "account_count": len(issue_rows),
                    "rows": issue_rows,
                }
            )
    return groups


def account_data_confidence(account_rows):
    deductions = {
        "missing_dates": {"points": 25, "reason": "Missing valuation date", "fix": "Edit account"},
        "stale": {"points": 20, "reason": "Stale valuation", "fix": "Refresh import"},
        "depot_drift": {"points": 20, "reason": "Depot holdings do not match account balance", "fix": "Review holdings"},
        "moneymoney": {"points": 20, "reason": "MoneyMoney source key missing", "fix": "Review mappings"},
        "duplicate_names": {"points": 5, "reason": "Duplicate display name", "fix": "Edit account"},
    }
    legend = [
        {"level": "high", "severity": "ok", "range": "90-100%", "detail": "Fresh and explainable enough for normal planning."},
        {"level": "medium", "severity": "warning", "range": "70-89%", "detail": "Usable, but review the listed drivers before relying on details."},
        {"level": "low", "severity": "critical", "range": "Below 70%", "detail": "Fix account data before trusting projections that depend on it."},
    ]
    account_scores = []
    for row in account_rows:
        score = 100
        applied_keys = set()
        deductions_applied = []
        notes = []
        action_urls = {action["label"]: action["url"] for action in row["actions"]}
        for issue in row["issues"]:
            key = issue["key"]
            if key in applied_keys:
                continue
            applied_keys.add(key)
            deduction = deductions.get(key)
            if deduction:
                score -= deduction["points"]
                deductions_applied.append(
                    {
                        "label": issue["label"],
                        "points": deduction["points"],
                        "reason": deduction["reason"],
                        "fix_label": deduction["fix"],
                        "fix_url": action_urls.get(deduction["fix"]),
                    }
                )
            else:
                notes.append({"label": issue["label"], "severity": issue["severity"]})
        score = max(score, 0)
        if score >= 90:
            level = "high"
            severity = "ok"
        elif score >= 70:
            level = "medium"
            severity = "warning"
        else:
            level = "low"
            severity = "critical"
        account_scores.append(
            {
                "account": row["account"],
                "score": score,
                "level": level,
                "severity": severity,
                "issues": row["issues"],
                "deductions": deductions_applied,
                "notes": notes,
            }
        )
    action_buckets = [
        {
            "key": "refresh",
            "label": "Needs refresh",
            "detail": "Missing or stale valuation dates.",
            "action_label": "Refresh import",
            "action_url": reverse("planner:import_center"),
            "accounts": [
                item
                for item in account_scores
                if any(deduction["reason"] in {"Missing valuation date", "Stale valuation"} for deduction in item["deductions"])
            ],
        },
        {
            "key": "mapping",
            "label": "Needs source mapping",
            "detail": "MoneyMoney accounts without a stable source key.",
            "action_label": "Review mappings",
            "action_url": reverse("planner:moneymoney_mappings"),
            "accounts": [
                item
                for item in account_scores
                if any(deduction["reason"] == "MoneyMoney source key missing" for deduction in item["deductions"])
            ],
        },
        {
            "key": "holdings",
            "label": "Needs holdings review",
            "detail": "Depot balances that do not reconcile to holdings.",
            "action_label": "Review holdings",
            "action_url": reverse("planner:holding_index"),
            "accounts": [
                item
                for item in account_scores
                if any(deduction["reason"] == "Depot holdings do not match account balance" for deduction in item["deductions"])
            ],
        },
    ]
    total = len(account_scores)
    average_score = round(sum(item["score"] for item in account_scores) / total) if total else 0
    return {
        "average_score": average_score,
        "high_count": len([item for item in account_scores if item["level"] == "high"]),
        "medium_count": len([item for item in account_scores if item["level"] == "medium"]),
        "low_count": len([item for item in account_scores if item["level"] == "low"]),
        "total": total,
        "account_scores": account_scores,
        "action_buckets": action_buckets,
        "legend": legend,
        "lowest_accounts": sorted(account_scores, key=lambda item: (item["score"], item["account"].name))[:5],
    }


def reconciliation_center(request):
    household = get_household()
    reconciliation = build_import_reconciliation(household)
    account_rows = account_reconciliation_rows(household)
    focus = request.GET.get("focus", "all")
    search_query = request.GET.get("q", "").strip()
    normalized_query = search_query.lower()
    focus_options = [
        ("all", "All"),
        ("stale", "Stale"),
        ("missing_dates", "Missing dates"),
        ("depot_drift", "Depot drift"),
        ("outside_net_worth", "Outside net worth"),
        ("duplicate_names", "Duplicate names"),
        ("moneymoney", "MoneyMoney"),
        ("manual", "Manual"),
    ]
    focus_keys = {key for key, _label in focus_options}
    if focus not in focus_keys:
        focus = "all"
    mapping_review = build_moneymoney_mapping_review(household)
    duplicate_name_count = len([row for row in account_rows if "Duplicate display name" in row["warnings"]])
    stale_count = len([row for row in account_rows if row["stale_days"] is not None and row["stale_days"] > 45])
    missing_date_count = len([row for row in account_rows if row["account"].as_of_date is None])
    external_count = len([row for row in account_rows if not row["account"].counts_in_household_net_worth])
    depot_difference_count = len([row for row in account_rows if row["depot_difference"]])
    source_counts = {}
    for row in account_rows:
        source_counts[row["account"].get_source_display()] = source_counts.get(row["account"].get_source_display(), 0) + 1
    focus_counts = {
        key: len(account_rows) if key == "all" else len([row for row in account_rows if key in row["issue_keys"]])
        for key, _label in focus_options
    }
    filtered_account_rows = []
    for row in account_rows:
        account = row["account"]
        if focus != "all" and focus not in row["issue_keys"]:
            continue
        if normalized_query:
            searchable = " ".join(
                [
                    account.name,
                    account.get_account_type_display(),
                    account.owner_label,
                    account.get_source_display(),
                    account.moneymoney_account_key,
                    account.institution,
                    " ".join(row["warnings"]),
                ]
            ).lower()
            if normalized_query not in searchable:
                continue
        filtered_account_rows.append(row)
    issue_groups = reconciliation_issue_groups(filtered_account_rows)
    data_confidence = account_data_confidence(filtered_account_rows)

    return render(
        request,
        "planner/reconciliation_center.html",
        {
            "household": household,
            "reconciliation": reconciliation,
            "account_rows": account_rows,
            "filtered_account_rows": filtered_account_rows,
            "issue_groups": issue_groups,
            "data_confidence": data_confidence,
            "mapping_review": mapping_review,
            "source_counts": source_counts,
            "focus_options": focus_options,
            "focus_counts": focus_counts,
            "selected_focus": focus,
            "search_query": search_query,
            "duplicate_name_count": duplicate_name_count,
            "stale_count": stale_count,
            "missing_date_count": missing_date_count,
            "external_count": external_count,
            "depot_difference_count": depot_difference_count,
        },
    )


def account_totals(accounts, household):
    # Thin wrapper kept for the view/template call sites; the rules live in one
    # place so the dashboard, snapshots, and the projection can't drift.
    return current_balance_sheet(household, accounts)


def family_gift_allowance_rows(gifts):
    rows = {}
    for gift in gifts:
        key = (
            gift.giver_id,
            gift.recipient_id,
            gift.window_start_year,
            gift.window_end_year,
        )
        if key not in rows:
            rows[key] = {
                "giver": gift.giver,
                "recipient": gift.recipient,
                "window_start_year": gift.window_start_year,
                "window_end_year": gift.window_end_year,
                "allowance_amount": gift.allowance_amount,
                "planned_amount": Decimal("0.00"),
            }
        rows[key]["planned_amount"] += gift.amount
        rows[key]["allowance_amount"] = max(rows[key]["allowance_amount"], gift.allowance_amount)
    for row in rows.values():
        row["remaining_amount"] = row["allowance_amount"] - row["planned_amount"]
        row["over_allowance"] = row["remaining_amount"] < 0
    return sorted(rows.values(), key=lambda row: (row["window_start_year"], row["recipient"].name, row["giver"].name))


def retirement_plan_summary(plan, household):
    months_until_retirement = max(
        0,
        (plan.retirement_start_month.year - household.start_month.year) * 12
        + plan.retirement_start_month.month
        - household.start_month.month,
    )
    years_until_retirement = Decimal(months_until_retirement) / Decimal("12")
    projected_points = plan.current_pension_points + (plan.expected_annual_points * years_until_retirement)
    statutory_at_start = projected_points * plan.pension_value_per_point
    total_at_start = statutory_at_start + plan.private_monthly_pension
    return {
        "plan": plan,
        "months_until_retirement": months_until_retirement,
        "years_until_retirement": years_until_retirement,
        "projected_points": projected_points,
        "statutory_at_start": statutory_at_start,
        "total_at_start": total_at_start,
    }


def retirement_summary(household):
    plans = list(household.retirement_plans.select_related("person"))
    plan_rows = [retirement_plan_summary(plan, household) for plan in plans]
    total_current_statutory = sum((plan.monthly_pension_from_current_points for plan in plans), Decimal("0.00"))
    total_private = sum((plan.private_monthly_pension for plan in plans), Decimal("0.00"))
    total_at_start = sum((row["total_at_start"] for row in plan_rows), Decimal("0.00"))
    active_people = household.people.filter(role=Person.Role.ADULT)
    return {
        "plan_rows": plan_rows,
        "total_current_statutory": total_current_statutory,
        "total_private": total_private,
        "total_at_start": total_at_start,
        "adult_count": active_people.count(),
    }


def build_fire_headline(yearly_projection, safe_draw_rate=SAFE_PORTFOLIO_DRAW_RATE):
    """Find the first year the cash goal is covered by income or a modest depot draw."""
    years_with_goal = [item for item in yearly_projection if item.annual_cash_goal > 0]
    if not years_with_goal:
        return {
            "status": "no_cash_goal",
            "label": "No cash goal",
            "detail": "Add yearly cash goals to calculate a FIRE date.",
            "safe_draw_rate": safe_draw_rate,
        }

    for item in years_with_goal:
        if item.cash_goal_gap <= 0:
            return {
                "status": "income_covered",
                "year": item.year,
                "label": item.label,
                "detail": "Planned income covers the yearly cash goal.",
                "draw_need": item.cash_goal_gap,
                "draw_percent": Decimal("0.00"),
                "safe_draw_rate": safe_draw_rate,
            }
        if (
            item.cash_goal_gap > 0
            and item.opening_invested_balance > 0
            and item.portfolio_draw_percent <= safe_draw_rate
        ):
            return {
                "status": "portfolio_supported",
                "year": item.year,
                "label": item.label,
                "detail": "Income plus a portfolio draw is within the safe draw threshold.",
                "draw_need": item.cash_goal_gap,
                "draw_percent": item.portfolio_draw_percent,
                "safe_draw_rate": safe_draw_rate,
            }

    closest = min(
        years_with_goal,
        key=lambda item: item.portfolio_draw_percent if item.opening_invested_balance > 0 else Decimal("999999.00"),
    )
    return {
        "status": "not_reached",
        "label": "Not in horizon",
        "detail": "No projected year covers the cash goal within the safe draw threshold.",
        "draw_need": closest.cash_goal_gap,
        "draw_percent": closest.portfolio_draw_percent,
        "safe_draw_rate": safe_draw_rate,
    }


def projection_navigation(index, total):
    return {
        "previous_index": index - 1 if index > 0 else None,
        "next_index": index + 1 if index < total - 1 else None,
    }


def forecast_display_mode(request):
    return "real" if request.GET.get("display") == "real" else "nominal"


def privacy_mode_toggle(request):
    if request.method != "POST":
        return redirect("planner:dashboard")
    enabled = request.POST.get("enabled") == "1"
    request.session[PRIVACY_MODE_SESSION_KEY] = enabled
    messages.success(request, "Privacy mode is now on." if enabled else "Privacy mode is now off.")
    target = _safe_post_redirect_target(request)
    if _is_ingress_target(request, target):
        target = _with_query_param(target, settings.LIF_PRIVACY_QUERY_PARAM, "1" if enabled else "0")
    return redirect(target)


def _safe_post_redirect_target(request):
    fallback = _with_script_name(request, "/")
    target = request.POST.get("next") or request.META.get("HTTP_REFERER") or fallback
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


def _is_ingress_target(request, target):
    script_name = request.META.get("SCRIPT_NAME", "").rstrip("/")
    if script_name:
        return True
    return urlsplit(target).path.startswith("/api/hassio_ingress/")


def _with_query_param(target, key, value):
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def dashboard(request):
    if not Household.objects.exists():
        messages.info(request, "Start with the local household foundation before entering real account data.")
        return redirect("planner:setup")

    household = get_household()
    display_mode = forecast_display_mode(request)
    projection, yearly_projection, display_projection, liquidity_view = build_display_projection(household)
    retirement_health_issues = build_retirement_health_issues(household, projection, yearly_projection)
    fire_headline = build_fire_headline(yearly_projection)
    display_granularity = household.resolved_display_granularity
    first_month = projection[0] if projection else None
    lowest_month = min(projection, key=lambda item: item.liquid_balance) if projection else None
    ending_month = projection[-1] if projection else None
    first_shortfall = next((item for item in projection if item.liquid_balance < 0), None)
    rules = household.rules.select_related("person", "account")
    rules_list = list(rules)
    emergency_target = emergency_fund_target(household, rules_list)
    emergency_expense_base = recurring_monthly_expenses(rules_list)
    income_rules = rules.filter(kind=MoneyRule.Kind.INCOME)
    transfer_rules = household.transfer_rules.select_related("person", "source_account", "target_account")
    accounts = household.accounts.prefetch_related("holdings")
    depot_holdings = [
        holding
        for account in accounts
        if account.account_type == AssetAccount.AccountType.DEPOT
        for holding in account.holdings.all()
    ]
    totals = account_totals(accounts, household)
    debts = household.debts.select_related("account", "source_account")
    properties = household.properties.select_related("source_account", "sale_proceeds_account").prefetch_related("debts")
    retirement_plans = household.retirement_plans.select_related("person")
    scenarios = household.scenarios.all()
    cash_goals = household.cash_goals.all()
    dashboard_account_rows = account_reconciliation_rows(household)
    dashboard_data_confidence = account_data_confidence(dashboard_account_rows)
    dashboard_lowest_confidence_account = (
        dashboard_data_confidence["lowest_accounts"][0] if dashboard_data_confidence["lowest_accounts"] else None
    )
    dashboard_assumption_registry = build_assumption_registry(
        household,
        reviews=list(household.assumption_reviews.all()),
    )
    latest_snapshot_review = household.snapshot_reviews.select_related("baseline_snapshot", "comparison_snapshot").first()
    baseline_snapshot = household.baseline_snapshot
    latest_review_summary = None
    if latest_snapshot_review:
        review_comparison = compare_snapshot_summaries(
            latest_snapshot_review.baseline_snapshot.summary,
            latest_snapshot_review.comparison_snapshot.summary,
        )
        net_worth_delta = next((row for row in review_comparison["totals"] if row["key"] == "net_worth"), None)
        latest_review_summary = {
            "review": latest_snapshot_review,
            "net_worth_delta": net_worth_delta,
            "currency": review_comparison["currency"],
        }
    open_review_actions = list(SnapshotReviewAction.objects.filter(
        review__household=household,
        status=SnapshotReviewAction.Status.OPEN,
    ).select_related("review", "owner")[:5])
    dashboard_integrity = check_projection_integrity(projection, yearly_projection, accounts)
    readiness_items = []
    if dashboard_data_confidence["average_score"] < 90:
        readiness_items.append(
            {
                "label": "Account foundation",
                "detail": f"{dashboard_data_confidence['average_score']}% data confidence.",
                "url": reverse("planner:reconciliation_center"),
            }
        )
    expired_assumptions = dashboard_assumption_registry["confidence_counts"].get("expired", 0)
    if expired_assumptions:
        readiness_items.append(
            {
                "label": "Assumption reviews",
                "detail": f"{expired_assumptions} expired assumption review(s).",
                "url": reverse("planner:assumption_review_center"),
            }
        )
    if not dashboard_integrity["ok"]:
        readiness_items.append(
            {
                "label": "Projection integrity",
                "detail": f"{dashboard_integrity['failure_count']} integrity failure(s).",
                "url": reverse("planner:projection_integrity"),
            }
        )
    if open_review_actions:
        readiness_items.append(
            {
                "label": "Review actions",
                "detail": f"{len(open_review_actions)} open annual-review action(s).",
                "url": reverse("planner:snapshot_review"),
            }
        )
    if not readiness_items:
        decision_readiness = {
            "status": "ready",
            "severity": "ok",
            "label": "Decision-ready",
            "detail": "Data confidence, assumptions, projection integrity, and review actions look clear.",
            "items": [],
        }
    elif dashboard_integrity["failure_count"] or dashboard_data_confidence["average_score"] < 70:
        decision_readiness = {
            "status": "blocked",
            "severity": "critical",
            "label": "Fix before decisions",
            "detail": "Core data or projection integrity needs attention.",
            "items": readiness_items,
        }
    else:
        decision_readiness = {
            "status": "review",
            "severity": "warning",
            "label": "Review before decisions",
            "detail": "A few checks should be reviewed before using this forecast for household decisions.",
            "items": readiness_items,
        }
    scenario_cards = [
        {
            "scenario": scenario,
            "monthly_delta": scenario.monthly_income_delta - scenario.monthly_expense_delta,
        }
        for scenario in scenarios
        if scenario.is_active
    ]

    return render(
        request,
        "planner/dashboard.html",
        {
            "household": household,
            "accounts": accounts,
            "depot_holdings": depot_holdings,
            **totals,
            "debts": debts,
            "properties": properties,
            "retirement_plans": retirement_plans,
            "scenario_cards": scenario_cards,
            "cash_goals": cash_goals,
            "data_confidence": dashboard_data_confidence,
            "decision_readiness": decision_readiness,
            "lowest_confidence_account": dashboard_lowest_confidence_account,
            "assumption_registry": dashboard_assumption_registry,
            "projection_integrity": dashboard_integrity,
            "latest_review_summary": latest_review_summary,
            "baseline_snapshot": baseline_snapshot,
            "open_review_actions": open_review_actions,
            "income_rules": income_rules,
            "transfer_rules": transfer_rules,
            "emergency_target": emergency_target,
            "emergency_gap": max(emergency_target - totals["liquid_total"], Decimal("0.00")),
            "emergency_expense_base": emergency_expense_base,
            "display_projection": display_projection,
            "display_mode": display_mode,
            "display_granularity": display_granularity,
            "liquidity_view": liquidity_view,
            "first_month": first_month,
            "lowest_month": lowest_month,
            "ending_month": ending_month,
            "first_shortfall": first_shortfall,
            "fire_headline": fire_headline,
            "retirement_health_issues": retirement_health_issues,
        },
    )


def data_quality(request):
    household = get_household()
    report = build_quality_report(household)
    assumption_registry = build_assumption_registry(household, reviews=list(household.assumption_reviews.all()))
    selected_category = request.GET.get("category", "")
    selected_severity = request.GET.get("severity", "")
    filtered_issues = report["issues"]
    if selected_category:
        filtered_issues = [item for item in filtered_issues if item.category == selected_category]
    if selected_severity:
        filtered_issues = [item for item in filtered_issues if item.severity == selected_severity]
    filtered_groups = []
    for group in report["grouped_issues"]:
        group_issues = [item for item in group["issues"] if item in filtered_issues]
        if group_issues:
            filtered_groups.append({"category": group["category"], "count": len(group_issues), "issues": group_issues})
    return render(
        request,
        "planner/data_quality.html",
        {
            "household": household,
            "filtered_issues": filtered_issues,
            "filtered_groups": filtered_groups,
            "selected_category": selected_category,
            "selected_severity": selected_severity,
            "filtered_total": len(filtered_issues),
            "assumption_registry": assumption_registry,
            **report,
        },
    )


def planning_review_checklist(request):
    household = get_household()
    quality_report = build_quality_report(household)
    assumption_registry = build_assumption_registry(household, reviews=list(household.assumption_reviews.all()))
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action != "mark_expired_assumptions_reviewed":
            raise Http404("Review action not found.")
        reviewed_by = request.POST.get("reviewed_by", "").strip()
        note = request.POST.get("note", "").strip()
        expired_rows = assumption_registry["expired_rows"]
        for row in expired_rows:
            AssumptionReview.objects.update_or_create(
                household=household,
                key=row["review_key"],
                defaults={"label": row["label"], "reviewed_by": reviewed_by, "note": note},
            )
        messages.success(request, f"Marked {len(expired_rows)} expired assumption review(s) current.")
        return redirect("planner:planning_review_checklist")
    data_confidence = account_data_confidence(account_reconciliation_rows(household))
    integrity = quality_report.get("integrity", {"ok": False, "failure_count": 0, "checked": 0})
    items = [
        {
            "label": "Account data confidence",
            "complete": data_confidence["average_score"] >= 90,
            "detail": f"{data_confidence['average_score']}% average account confidence.",
            "url": reverse("planner:reconciliation_center"),
            "action": "Review reconciliation",
        },
        {
            "label": "Assumption reviews",
            "complete": assumption_registry["confidence_counts"].get("expired", 0) == 0,
            "detail": f"{assumption_registry['confidence_counts'].get('expired', 0)} expired review(s).",
            "url": reverse("planner:assumption_review_center"),
            "action": "Review assumptions",
        },
        {
            "label": "Projection integrity",
            "complete": integrity.get("ok", False),
            "detail": f"{integrity.get('failure_count', 0)} integrity failure(s) across {integrity.get('checked', 0)} checks.",
            "url": reverse("planner:projection_integrity"),
            "action": "Open integrity",
        },
        {
            "label": "Data quality",
            "complete": quality_report["counts"]["critical"] == 0,
            "detail": f"{quality_report['counts']['critical']} critical, {quality_report['counts']['warning']} warning issue(s).",
            "url": reverse("planner:data_quality"),
            "action": "Open quality",
        },
        {
            "label": "Scenario confidence",
            "complete": data_confidence["average_score"] >= 90 and integrity.get("ok", False),
            "detail": "Scenario comparison uses the same account foundation and projection math.",
            "url": reverse("planner:scenario_compare"),
            "action": "Compare scenarios",
        },
    ]
    complete_count = len([item for item in items if item["complete"]])
    return render(
        request,
        "planner/planning_review_checklist.html",
        {
            "household": household,
            "items": items,
            "complete_count": complete_count,
            "total_count": len(items),
            "data_confidence": data_confidence,
            "assumption_registry": assumption_registry,
            "quality_report": quality_report,
        },
    )


def change_history(request):
    household = get_household()
    entries = ChangeLogEntry.objects.filter(household=household)
    selected_model = request.GET.get("model", "")
    selected_action = request.GET.get("action", "")
    if selected_model:
        entries = entries.filter(model_name=selected_model)
    if selected_action:
        entries = entries.filter(action=selected_action)
    model_options = (
        ChangeLogEntry.objects.filter(household=household)
        .values_list("model_name", flat=True)
        .distinct()
        .order_by("model_name")
    )
    return render(
        request,
        "planner/change_history.html",
        {
            "household": household,
            "entries": entries[:100],
            "model_options": model_options,
            "action_options": ChangeLogEntry.Action.choices,
            "selected_model": selected_model,
            "selected_action": selected_action,
        },
    )


def _change_log_display(mapping, field_name):
    if field_name not in mapping:
        return "-"
    value = mapping[field_name]
    return "-" if value is None else value


def change_history_detail(request, pk):
    household = get_household()
    entry = get_object_or_404(ChangeLogEntry, pk=pk, household=household)
    field_names = entry.changed_fields or sorted(set(entry.before) | set(entry.after))
    field_rows = [
        {
            "field": field_name,
            "before": _change_log_display(entry.before, field_name),
            "after": _change_log_display(entry.after, field_name),
        }
        for field_name in field_names
    ]
    return render(
        request,
        "planner/change_history_detail.html",
        {
            "household": household,
            "entry": entry,
            "field_rows": field_rows,
        },
    )


def attention_settings(request):
    from .context_processors import build_attention_items, hidden_item_active

    household = get_household()
    flags = feature_flag_map()
    all_current_items = build_attention_items(household, flags, include_hidden=True)
    hidden = household.hidden_attention_items or {}
    active_items = [
        item
        for item in all_current_items
        if item["key"] not in hidden or not hidden_item_active(hidden[item["key"]])
    ]
    hidden_current_items = [
        {**item, **hidden.get(item["key"], {})}
        for item in all_current_items
        if item["key"] in hidden and hidden_item_active(hidden[item["key"]])
    ]
    current_hidden_keys = {item["key"] for item in hidden_current_items}
    stale_hidden_items = [
        {"key": key, **value}
        for key, value in hidden.items()
        if key not in current_hidden_keys
    ]
    return render(
        request,
        "planner/attention_settings.html",
        {
            "household": household,
            "active_items": active_items,
            "hidden_items": hidden_current_items,
            "stale_hidden_items": stale_hidden_items,
            "hidden_count": len(hidden_current_items) + len(stale_hidden_items),
            "critical_count": len([item for item in active_items if item["priority"] == "critical"]),
        },
    )


def attention_hide(request):
    if request.method != "POST":
        raise Http404("Attention item not found.")
    from .context_processors import build_attention_items

    household = get_household()
    key = request.POST.get("key", "")
    items = {item["key"]: item for item in build_attention_items(household, feature_flag_map(), include_hidden=True)}
    item = items.get(key)
    if not item or not item.get("dismissible", True):
        messages.error(request, "This attention item cannot be hidden.")
        return redirect(request.META.get("HTTP_REFERER") or "planner:dashboard")

    mode = request.POST.get("mode", "hide")
    hidden_until = ""
    if mode == "snooze_30":
        hidden_until = (timezone.localdate() + timedelta(days=30)).isoformat()
    elif mode == "snooze_90":
        hidden_until = (timezone.localdate() + timedelta(days=90)).isoformat()
    elif mode == "snooze_review":
        today = timezone.localdate()
        review_year = today.year + 1 if today.month >= 11 else today.year
        hidden_until = date(review_year, 12, 31).isoformat()
    hidden = dict(household.hidden_attention_items or {})
    hidden[key] = {
        "label": item["label"],
        "detail": item["detail"],
        "severity": item["severity"],
        "priority": item["priority"],
        "action_label": item["action_label"],
        "hidden_until": hidden_until,
    }
    household.hidden_attention_items = hidden
    household.save(update_fields=["hidden_attention_items", "updated_at"])
    messages.success(
        request,
        format_html(
            '{} {} from the attention rail. <a href="{}">Manage hidden items</a>.',
            item["label"],
            "snoozed" if mode.startswith("snooze") else "hidden",
            reverse("planner:attention_settings"),
        ),
    )
    return redirect(request.META.get("HTTP_REFERER") or "planner:dashboard")


def attention_restore(request):
    if request.method != "POST":
        raise Http404("Attention item not found.")
    household = get_household()
    key = request.POST.get("key", "")
    hidden = dict(household.hidden_attention_items or {})
    restored = hidden.pop(key, None)
    household.hidden_attention_items = hidden
    household.save(update_fields=["hidden_attention_items", "updated_at"])
    if restored:
        messages.success(request, f"{restored.get('label', 'Attention item')} restored.")
    return redirect("planner:attention_settings")


def onboarding(request):
    household = get_household()
    readiness = build_household_readiness(household, request.session.get("moneymoney_diagnostics"))
    steps = [
        item
        for section in readiness["sections"]
        for item in section["items"]
    ]
    return render(
        request,
        "planner/onboarding.html",
        {
            "household": household,
            "steps": steps,
            "summary": readiness["summary"],
        },
    )


def real_data_start(request):
    household = get_household()
    baseline_snapshot = household.baseline_snapshot
    readiness = build_household_readiness(household, request.session.get("moneymoney_diagnostics"))
    items_by_key = {
        item["key"]: item
        for section in readiness["sections"]
        for item in section["items"]
    }
    workflow = [
        {
            "title": "1. Prepare the real household",
            "detail": "Separate real planning data from demo exploration before entering private numbers.",
            "items": [items_by_key[key] for key in ["real_mode", "real_name", "backup"] if key in items_by_key],
        },
        {
            "title": "2. Enter the household foundation",
            "detail": "Add the people, income, costs, accounts, debts, pensions, and yearly cash goals that drive the forecast.",
            "items": [
                items_by_key[key]
                for key in ["adults", "accounts", "income", "expenses", "cash_goals", "depot_holdings", "mortgages", "retirement"]
                if key in items_by_key
            ],
        },
        {
            "title": "3. Import and reconcile current values",
            "detail": "Use preview-first imports and the runbook before applying account or depot data.",
            "items": [items_by_key[key] for key in ["preview_import", "applied_import", "runbook"] if key in items_by_key],
        },
        {
            "title": "4. Verify and freeze day zero",
            "detail": "Review completeness and create the first snapshot once the forecast looks trustworthy.",
            "items": [
                items_by_key["quality"],
                {
                    "key": "snapshot",
                    "label": "Create first real-data snapshot",
                    "complete": household.snapshots.exists(),
                    "detail": f"{household.snapshots.count()} snapshot(s) recorded.",
                    "action_url": reverse("planner:snapshots"),
                    "action_label": "Create snapshot",
                },
            ],
        },
    ]
    baseline_snapshot_prompt = (
        feature_flag_map().get("snapshots")
        and household.data_mode == Household.DataMode.REAL
        and not household.snapshots.exists()
        and readiness["quality_report"]["counts"]["critical"] == 0
        and readiness["summary"]["percent"] >= 70
    )
    return render(
        request,
        "planner/real_data_start.html",
        {
            "household": household,
            "workflow": workflow,
            "summary": readiness["summary"],
            "quality_report": readiness["quality_report"],
            "baseline_snapshot_prompt": baseline_snapshot_prompt,
            "baseline_snapshot": baseline_snapshot,
        },
    )


def plan_index(request):
    household = get_household()
    rules = household.rules.select_related("person", "account")
    income_rules = list(rules.filter(kind=MoneyRule.Kind.INCOME))
    expense_rules = list(rules.filter(kind=MoneyRule.Kind.EXPENSE))
    transfer_rules = list(household.transfer_rules.select_related("person", "source_account", "target_account"))
    planned_purchases = list(
        household.planned_investment_purchases.select_related("person", "source_account", "target_account")
    )
    family_gifts = list(
        household.family_gift_plans.select_related("giver", "recipient", "source_account", "target_account")
    )
    property_transfers = list(
        household.real_estate_transfer_plans.select_related("property_item", "giver", "recipient")
    )
    monthly_income = sum(rule.monthly_amount for rule in income_rules if rule.is_active)
    monthly_expenses = sum(rule.monthly_amount for rule in expense_rules if rule.is_active)
    monthly_transfers = sum(rule.monthly_amount for rule in transfer_rules if rule.is_active)
    categories = sorted({rule.category for rule in [*income_rules, *expense_rules] if rule.category})
    true_expenses = household.true_expenses.select_related("account")
    income_investments = household.income_investments.all()
    private_loans = household.private_loans.select_related("source_account")
    equity_grants = household.equity_grants.select_related("person", "account")
    child_milestones = ChildMilestone.objects.filter(person__household=household).select_related("person")
    salary_changes = SalaryChange.objects.filter(person__household=household).select_related("person", "account")
    has_future_changes = any(
        queryset.exists()
        for queryset in [
            true_expenses,
            income_investments,
            private_loans,
            equity_grants,
            child_milestones,
            salary_changes,
            household.planned_investment_purchases,
            household.family_gift_plans,
            household.real_estate_transfer_plans,
        ]
    )
    return render(
        request,
        "planner/plan_index.html",
        {
            "household": household,
            "income_rules": income_rules,
            "expense_rules": expense_rules,
            "transfer_rules": transfer_rules,
            "planned_purchases": planned_purchases,
            "family_gifts": family_gifts,
            "property_transfers": property_transfers,
            "family_gift_allowance_rows": family_gift_allowance_rows(
                [gift for gift in family_gifts if gift.is_active]
            ),
            "monthly_income": monthly_income,
            "monthly_expenses": monthly_expenses,
            "monthly_transfers": monthly_transfers,
            "monthly_net": monthly_income - monthly_expenses,
            "rule_categories": categories,
            "true_expenses": true_expenses,
            "income_investments": income_investments,
            "private_loans": private_loans,
            "equity_grants": equity_grants,
            "child_milestones": child_milestones,
            "salary_changes": salary_changes,
            "has_future_changes": has_future_changes,
        },
    )


def account_index(request):
    household = get_household()
    accounts = list(household.accounts.prefetch_related("holdings"))
    planning_accounts = [account for account in accounts if account.counts_in_household_net_worth]
    child_tracked_total = sum(
        (account.effective_balance for account in accounts if not account.counts_in_household_net_worth),
        Decimal("0.00"),
    )
    today = timezone.localdate()
    stale_accounts = [
        account
        for account in accounts
        if account.as_of_date and (today - account.as_of_date).days > 45
    ]
    depot_warnings = [
        {
            "account": account,
            "difference": account.depot_difference,
        }
        for account in accounts
        if account.account_type == AssetAccount.AccountType.DEPOT and account.holdings.exists() and account.depot_difference
    ]
    grouped_totals = [
        {
            "label": label,
            "value": sum(account.effective_balance for account in planning_accounts if account.account_type == account_type),
        }
        for account_type, label in [
            (AssetAccount.AccountType.CASH, "Cash"),
            (AssetAccount.AccountType.SAVINGS, "Savings"),
            (AssetAccount.AccountType.DEPOT, "Depot"),
            (AssetAccount.AccountType.OTHER, "Other assets"),
            (AssetAccount.AccountType.LOAN, "Loans"),
        ]
    ]
    return render(
        request,
        "planner/account_index.html",
        {
            "household": household,
            "accounts": accounts,
            "stale_accounts": stale_accounts,
            "depot_warnings": depot_warnings,
            "grouped_totals": grouped_totals,
            "child_tracked_total": child_tracked_total,
            **account_totals(accounts, household),
        },
    )


@feature_required("cash_goals")
def cash_goal_index(request):
    household = get_household()
    return render(
        request,
        "planner/cash_goal_index.html",
        {
            "household": household,
            "cash_goals": household.cash_goals.all(),
        },
    )


@feature_required("depot_holdings")
def holding_index(request):
    household = get_household()
    holdings = DepotHolding.objects.filter(asset_account__household=household).select_related("asset_account")
    return render(
        request,
        "planner/holding_index.html",
        {
            "household": household,
            "holdings": holdings,
        },
    )


@feature_required("debts")
def debt_index(request):
    household = get_household()
    start = first_of_month(household.start_month)
    debts = household.debts.select_related("account")
    debt_rows = [
        {"debt": debt, "summary": summarize_debt(debt, start) if debt.is_active else None}
        for debt in debts
    ]
    return render(
        request,
        "planner/debt_index.html",
        {
            "household": household,
            "debt_rows": debt_rows,
        },
    )


@feature_required("real_estate")
def real_estate_index(request):
    household = get_household()
    properties = household.properties.select_related("source_account", "sale_proceeds_account").prefetch_related("debts")
    transfer_plans = household.real_estate_transfer_plans.select_related("property_item", "giver", "recipient")
    return render(
        request,
        "planner/real_estate_index.html",
        {
            "household": household,
            "properties": properties,
            "transfer_plans": transfer_plans,
        },
    )


@feature_required("retirement_plans")
def retirement_plan_index(request):
    household = get_household()
    plans = household.retirement_plans.select_related("person")
    return render(
        request,
        "planner/retirement_plan_index.html",
        {
            "household": household,
            "plans": plans,
        },
    )


def transfer_plan(request):
    household = get_household()
    projection = build_projection(household)
    transfer_rules = list(household.transfer_rules.select_related("person", "source_account", "target_account"))
    accounts = {account.id: account for account in household.accounts.all()}
    event_rows = []
    warnings = []
    transfer_sections = {"Transfer", "Extra repayment"}
    for rule in transfer_rules:
        for month in projection:
            if not rule_applies(rule, month.month, projection_start=first_of_month(household.start_month)):
                continue
            line = next(
                (
                    audit_line
                    for audit_line in month.audit_lines
                    if audit_line.name == rule.name and audit_line.section in transfer_sections
                ),
                None,
            )
            if line is None:
                continue
            source_balance = month.account_balances.get(rule.source_account_id) if rule.source_account_id else None
            target_balance = month.account_balances.get(rule.target_account_id)
            row = {
                "rule": rule,
                "month": month,
                "amount": line.amount,
                "section": line.section,
                "note": line.note,
                "source_balance": source_balance,
                "target_balance": target_balance,
                "source_negative": source_balance is not None and source_balance < Decimal("0.00"),
                "target_account": accounts.get(rule.target_account_id),
            }
            if row["source_negative"]:
                warnings.append(row)
            event_rows.append(row)
    account_rows = []
    for account in accounts.values():
        account_points = [
            {"month": item.month, "index": item.index, "balance": item.account_balances.get(account.id)}
            for item in projection
            if account.id in item.account_balances
        ]
        if not account_points:
            continue
        lowest = min(account_points, key=lambda item: item["balance"])
        account_rows.append(
            {
                "account": account,
                "opening": account.effective_balance,
                "ending": account_points[-1]["balance"],
                "lowest": lowest,
            }
        )
    # Global cash-flow ledger: every money movement, paginated by calendar year
    # and filterable by account and category.
    all_ledger_rows = cash_flow_ledger_rows(projection)
    ledger_years = sorted({row["year"] for row in all_ledger_rows})
    try:
        selected_year = int(request.GET.get("year"))
    except (TypeError, ValueError):
        selected_year = None
    if selected_year not in ledger_years:
        selected_year = ledger_years[0] if ledger_years else None
    selected_account = request.GET.get("account", "all")
    selected_group = request.GET.get("group", "all")
    ledger_query = request.GET.get("q", "").strip()
    normalized_query = ledger_query.lower()
    account_names = {account.id: account.name for account in accounts.values()}
    ledger_rows = []
    for row in all_ledger_rows:
        if row["year"] != selected_year:
            continue
        if selected_account == "general" and row["account_id"] is not None:
            continue
        if selected_account not in ("all", "general") and str(row["account_id"]) != selected_account:
            continue
        if selected_group != "all" and row["group_key"] != selected_group:
            continue
        account_label = account_names.get(row["account_id"], "General pool")
        if normalized_query:
            searchable_text = " ".join(
                str(value)
                for value in (
                    row["name"],
                    row.get("note", ""),
                    row["group_title"],
                    account_label,
                    row["amount"],
                )
            ).lower()
            if normalized_query not in searchable_text:
                continue
        ledger_rows.append({**row, "account_label": account_label})
    year_position = ledger_years.index(selected_year) if selected_year in ledger_years else 0
    prev_year = ledger_years[year_position - 1] if year_position > 0 else None
    next_year = ledger_years[year_position + 1] if 0 <= year_position < len(ledger_years) - 1 else None
    account_options = [("all", "All accounts"), ("general", "General pool")] + [
        (str(account.id), account.name) for account in accounts.values()
    ]
    group_options = [("all", "All categories")] + [(group["key"], group["title"]) for group in GROUPS]

    return render(
        request,
        "planner/transfer_plan.html",
        {
            "household": household,
            "transfer_rules": transfer_rules,
            "event_rows": event_rows[:120],
            "warning_rows": warnings,
            "account_rows": account_rows,
            "ledger_rows": ledger_rows,
            "ledger_years": ledger_years,
            "selected_year": selected_year,
            "prev_year": prev_year,
            "next_year": next_year,
            "selected_account": selected_account,
            "selected_group": selected_group,
            "ledger_query": ledger_query,
            "account_options": account_options,
            "group_options": group_options,
        },
    )


def projection_monthly_export(request):
    household = get_household()
    projection = build_projection(household)
    return csv_response(
        f"{household.name.lower().replace(' ', '-')}-projection-monthly.csv",
        projection_month_headers(),
        projection_month_rows(projection),
    )


def projection_yearly_export(request):
    household = get_household()
    projection = build_projection(household)
    yearly_projection = build_yearly_projection(projection, household.cash_goals.all())
    return csv_response(
        f"{household.name.lower().replace(' ', '-')}-projection-yearly.csv",
        projection_year_headers(),
        projection_year_rows(yearly_projection),
    )


def cash_flow_export(request):
    household = get_household()
    projection = build_projection(household)
    accounts = {account.id: account for account in household.accounts.all()}
    return csv_response(
        f"{household.name.lower().replace(' ', '-')}-cash-flow-ledger.csv",
        cash_flow_headers(),
        cash_flow_rows(projection, accounts),
    )


def yearly_report(request):
    household = get_household()
    try:
        selected_year = int(request.GET.get("year"))
    except (TypeError, ValueError):
        selected_year = None
    report = build_report_year(household, selected_year)
    return render(
        request,
        "planner/yearly_report.html",
        {
            "household": household,
            "report": report,
        },
    )


def yearly_report_slides(request, year):
    household = get_household()
    report = build_report_year(household, year)
    if report["year"] is None:
        raise Http404("No report year available.")
    return render(
        request,
        "planner/yearly_report_slides.html",
        {
            "household": household,
            "report": report,
        },
    )


def account_trust_summary(account, forecast_rows, related_counts, holdings):
    today = timezone.localdate()
    stale_days = (today - account.as_of_date).days if account.as_of_date else None
    lowest_forecast = min(forecast_rows, key=lambda item: item["balance"]) if forecast_rows else None
    forecast_end = forecast_rows[-1] if forecast_rows else None
    opening_balance = -account.effective_balance if account.account_type == AssetAccount.AccountType.LOAN else account.effective_balance
    forecast_change = forecast_end["balance"] - opening_balance if forecast_end else None
    warnings = []

    if account.as_of_date is None:
        warnings.append(
            {
                "severity": "warning",
                "label": "No valuation date",
                "detail": "The account has no as-of date, so it is harder to tell whether the balance is current.",
            }
        )
    elif stale_days is not None and stale_days > 45:
        warnings.append(
            {
                "severity": "warning",
                "label": "Stale valuation",
                "detail": f"The balance is {stale_days} days old. Refresh the import or update the balance manually.",
            }
        )
    if not account.counts_in_household_net_worth:
        warnings.append(
            {
                "severity": "info",
                "label": "Tracked separately",
                "detail": "This account is visible here but excluded from household net worth and retirement planning.",
            }
        )
    if account.source == AssetAccount.Source.MONEYMONEY and not account.moneymoney_account_key:
        warnings.append(
            {
                "severity": "warning",
                "label": "MoneyMoney key missing",
                "detail": "The account is marked as imported from MoneyMoney but has no source key for stable matching.",
            }
        )
    if account.account_type == AssetAccount.AccountType.DEPOT and holdings and account.depot_difference:
        warnings.append(
            {
                "severity": "warning",
                "label": "Depot drift",
                "detail": "The account balance differs from the summed holding values.",
            }
        )
    if account.account_type == AssetAccount.AccountType.DEPOT and not holdings:
        warnings.append(
            {
                "severity": "info",
                "label": "No holdings",
                "detail": "Add individual holdings if you want this depot to reconcile and explain its value.",
            }
        )
    if lowest_forecast and lowest_forecast["balance"] < 0 and account.account_type != AssetAccount.AccountType.LOAN:
        warnings.append(
            {
                "severity": "critical",
                "label": "Projected negative balance",
                "detail": f"The 36-month account forecast drops below zero in {lowest_forecast['month']:%b %Y}.",
            }
        )

    actions = [
        {"label": "Edit account", "url": reverse("planner:account_update", args=[account.pk])},
        {
            "label": "Open reconciliation",
            "url": reverse("planner:reconciliation_center") + "?" + urlencode({"q": account.name}),
        },
    ]
    if account.as_of_date is None or (stale_days is not None and stale_days > 45):
        actions.append({"label": "Refresh import", "url": reverse("planner:import_center")})
    if account.account_type == AssetAccount.AccountType.DEPOT:
        actions.append({"label": "Review holdings", "url": reverse("planner:holding_index")})
    if account.source == AssetAccount.Source.MONEYMONEY or account.moneymoney_account_key:
        actions.append({"label": "Review mappings", "url": reverse("planner:moneymoney_mappings")})

    routed_item_count = sum(related_counts.values())
    if account.source == AssetAccount.Source.MONEYMONEY and not account.moneymoney_account_key:
        source_detail = "Missing MoneyMoney source key"
    else:
        source_detail = account.moneymoney_account_key or account.ynab_account_id or account.institution or "Manual/local entry"
    return {
        "actions": actions,
        "forecast_is_negative": bool(lowest_forecast and lowest_forecast["balance"] < 0),
        "forecast_change": forecast_change,
        "forecast_end": forecast_end,
        "is_stale": stale_days is not None and stale_days > 45,
        "lowest_forecast": lowest_forecast,
        "nonzero_related_counts": {label: count for label, count in related_counts.items() if count},
        "reconciliation_url": reverse("planner:reconciliation_center") + "?" + urlencode({"q": account.name}),
        "related_counts": related_counts,
        "routed_item_count": routed_item_count,
        "source_detail": source_detail,
        "stale_days": stale_days,
        "warnings": warnings,
    }


def account_ownership_summary(account, related_family_gifts):
    treatment_notes = []
    if account.owner_type == AssetAccount.OwnerType.PERSON and account.owner_person:
        treatment_notes.append(f"Legally assigned to {account.owner_person.name}.")
        if account.owner_person.role == Person.Role.CHILD:
            treatment_notes.append("Child-owned assets are useful for tracking gifts and Kinderdepots separately.")
    elif account.owner_type == AssetAccount.OwnerType.EXTERNAL:
        treatment_notes.append("Tracked for context only; treat this as informational unless you intentionally include it.")
    else:
        treatment_notes.append("Owned by the household and normally part of household planning.")
    if account.counts_in_household_net_worth:
        treatment_notes.append("Included in household net worth, liquidity, FIRE, and retirement planning totals.")
    else:
        treatment_notes.append("Excluded from household net worth, liquidity, FIRE, and retirement planning totals.")
    if related_family_gifts:
        treatment_notes.append("Linked family gift plans can move household cash into or out of this account.")
    return {
        "show": account.owner_type != AssetAccount.OwnerType.HOUSEHOLD or not account.counts_in_household_net_worth or bool(related_family_gifts),
        "notes": treatment_notes,
    }


def account_detail(request, pk):
    household = get_household()
    account = get_object_or_404(
        AssetAccount.objects.prefetch_related("holdings").select_related("debt"),
        pk=pk,
        household=household,
    )
    related_transfer_rules = household.transfer_rules.filter(
        Q(target_account=account) | Q(source_account=account)
    ).select_related("person", "source_account", "target_account")
    related_planned_purchases = household.planned_investment_purchases.filter(
        Q(target_account=account) | Q(source_account=account)
    ).select_related("person", "source_account", "target_account")
    related_money_rules = household.rules.filter(
        Q(account=account) | Q(account__isnull=True, household__default_operating_account=account)
    ).select_related("person", "account")
    related_true_expenses = household.true_expenses.filter(
        Q(account=account) | Q(account__isnull=True, household__default_operating_account=account)
    ).select_related("account")
    related_equity_grants = household.equity_grants.filter(
        Q(account=account) | Q(account__isnull=True, household__default_operating_account=account)
    ).select_related("person", "account")
    related_salary_changes = SalaryChange.objects.filter(
        Q(account=account) | Q(account__isnull=True, person__household__default_operating_account=account),
        person__household=household,
    ).select_related("person", "account")
    related_family_gifts = household.family_gift_plans.filter(
        Q(source_account=account) | Q(target_account=account)
    ).select_related("giver", "recipient", "source_account", "target_account")
    projection = build_projection(household)
    ledger_years = account_ledger_years(projection, account)
    try:
        selected_ledger_year = int(request.GET.get("ledger_year"))
    except (TypeError, ValueError):
        selected_ledger_year = None
    if selected_ledger_year not in ledger_years:
        selected_ledger_year = ledger_years[0] if ledger_years else None
    ledger_rows = account_ledger_rows(projection, account, year=selected_ledger_year)
    ledger_year_position = ledger_years.index(selected_ledger_year) if selected_ledger_year in ledger_years else 0
    prev_ledger_year = ledger_years[ledger_year_position - 1] if ledger_year_position > 0 else None
    next_ledger_year = (
        ledger_years[ledger_year_position + 1] if 0 <= ledger_year_position < len(ledger_years) - 1 else None
    )
    forecast_rows = [
        {
            "month": item.month,
            "index": item.index,
            "balance": item.account_balances.get(account.id),
        }
        for item in projection[:36]
        if account.id in item.account_balances
    ]
    previous_balance = -account.effective_balance if account.account_type == AssetAccount.AccountType.LOAN else account.effective_balance
    for row in forecast_rows:
        row["change"] = row["balance"] - previous_balance
        row["is_largest_increase"] = False
        row["is_largest_decrease"] = False
        previous_balance = row["balance"]
    increase_rows = [row for row in forecast_rows if row["change"] > 0]
    decrease_rows = [row for row in forecast_rows if row["change"] < 0]
    if increase_rows:
        max(increase_rows, key=lambda item: item["change"])["is_largest_increase"] = True
    if decrease_rows:
        min(decrease_rows, key=lambda item: item["change"])["is_largest_decrease"] = True
    lowest_forecast = min(forecast_rows, key=lambda item: item["balance"]) if forecast_rows else None
    account_holdings = list(account.holdings.all())
    related_counts = {
        "Recurring rules": related_money_rules.count(),
        "Transfers": related_transfer_rules.count(),
        "Planned purchases": related_planned_purchases.count(),
        "True expenses": related_true_expenses.count(),
        "Equity grants": related_equity_grants.count(),
        "Salary changes": related_salary_changes.count(),
        "Debts": household.debts.filter(Q(account=account) | Q(source_account=account)).count(),
        "Family gifts": household.family_gift_plans.filter(Q(source_account=account) | Q(target_account=account)).count(),
        "Income investments": household.income_investments.filter(source_account=account).count(),
        "Private loans": household.private_loans.filter(source_account=account).count(),
        "Properties": household.properties.filter(Q(source_account=account) | Q(sale_proceeds_account=account)).count(),
    }
    trust_summary = account_trust_summary(account, forecast_rows, related_counts, account_holdings)
    ownership_summary = account_ownership_summary(account, list(related_family_gifts))
    has_configured_distributions = (
        account.depot_annual_distribution_rate > 0
        or any(holding.annual_distribution_rate > 0 for holding in account_holdings)
    )
    return render(
        request,
        "planner/account_detail.html",
        {
            "household": household,
            "account": account,
            "holdings": account_holdings,
            "has_configured_distributions": has_configured_distributions,
            "related_transfer_rules": related_transfer_rules,
            "related_planned_purchases": related_planned_purchases,
            "related_money_rules": related_money_rules,
            "related_true_expenses": related_true_expenses,
            "related_equity_grants": related_equity_grants,
            "related_salary_changes": related_salary_changes,
            "related_family_gifts": related_family_gifts,
            "ownership_summary": ownership_summary,
            "debt": getattr(account, "debt", None),
            "ledger_rows": ledger_rows,
            "ledger_years": ledger_years,
            "selected_ledger_year": selected_ledger_year,
            "prev_ledger_year": prev_ledger_year,
            "next_ledger_year": next_ledger_year,
            "forecast_rows": forecast_rows,
            "lowest_forecast": lowest_forecast,
            "trust_summary": trust_summary,
        },
    )


def account_statement_export(request, pk):
    household = get_household()
    account = get_object_or_404(AssetAccount, pk=pk, household=household)
    projection = build_projection(household)
    return csv_response(
        f"{household.name.lower().replace(' ', '-')}-{account.name.lower().replace(' ', '-')}-statement.csv",
        statement_headers(),
        account_statement_rows(projection, account),
    )


def account_delete_blockers(account):
    blockers = []
    holdings_count = account.holdings.count()
    if holdings_count:
        blockers.append(f"{holdings_count} depot holding(s) are attached to this account.")
    if hasattr(account, "debt"):
        blockers.append("A debt plan is linked to this loan account.")
    incoming_transfer_count = account.incoming_transfer_rules.count()
    if incoming_transfer_count:
        blockers.append(f"{incoming_transfer_count} transfer rule(s) target this account.")
    planned_purchase_count = account.planned_investment_purchases.count()
    if planned_purchase_count:
        blockers.append(f"{planned_purchase_count} planned investment purchase(s) target this account.")
    return blockers


def account_delete(request, pk):
    household = get_household()
    account = get_object_or_404(AssetAccount, pk=pk, household=household)
    blockers = account_delete_blockers(account)
    affected_counts = {
        "money_rules": account.money_rules.count(),
        "true_expenses": account.true_expenses.count(),
        "equity_grants": account.equity_grants.count(),
        "salary_changes": SalaryChange.objects.filter(account=account, person__household=household).count(),
        "outgoing_transfers": account.outgoing_transfer_rules.count(),
        "funded_purchases": account.funded_investment_purchases.count(),
        "paid_debts": account.paid_debts.count(),
        "income_investments": account.funded_income_investments.count(),
        "private_loans": account.private_loan_receivables.count(),
        "properties": account.funded_properties.count() + account.property_sale_proceeds.count(),
    }
    if request.method == "POST" and not blockers:
        name = account.name
        account.delete()
        messages.success(request, f"{name} deleted locally. This does not run or schedule a new import.")
        return redirect("planner:account_index")
    if request.method == "POST":
        messages.error(request, "Account was not deleted because it is still used by planning records.")
    return render(
        request,
        "planner/account_confirm_delete.html",
        {
            "household": household,
            "account": account,
            "blockers": blockers,
            "affected_counts": {key: value for key, value in affected_counts.items() if value},
        },
    )


def general_pool_detail(request):
    household = get_household()
    projection = build_projection(household)
    ledger_years = general_pool_ledger_years(projection)
    try:
        selected_ledger_year = int(request.GET.get("ledger_year"))
    except (TypeError, ValueError):
        selected_ledger_year = None
    if selected_ledger_year not in ledger_years:
        selected_ledger_year = ledger_years[0] if ledger_years else None
    ledger_rows = general_pool_ledger_rows(projection, household, year=selected_ledger_year)
    ledger_year_position = ledger_years.index(selected_ledger_year) if selected_ledger_year in ledger_years else 0
    prev_ledger_year = ledger_years[ledger_year_position - 1] if ledger_year_position > 0 else None
    next_ledger_year = (
        ledger_years[ledger_year_position + 1] if 0 <= ledger_year_position < len(ledger_years) - 1 else None
    )
    current_balance = ledger_rows[-1]["ending_balance"] if ledger_rows else Decimal("0.00")
    return render(
        request,
        "planner/general_pool_detail.html",
        {
            "household": household,
            "ledger_rows": ledger_rows,
            "ledger_years": ledger_years,
            "selected_ledger_year": selected_ledger_year,
            "prev_ledger_year": prev_ledger_year,
            "next_ledger_year": next_ledger_year,
            "current_balance": current_balance,
        },
    )


def general_pool_statement_export(request):
    household = get_household()
    projection = build_projection(household)
    return csv_response(
        f"{household.name.lower().replace(' ', '-')}-general-liquid-pool-statement.csv",
        statement_headers("pool"),
        general_pool_statement_rows(projection, household),
    )


@feature_required("retirement_plans")
def retirement_detail(request):
    household = get_household()
    projection, yearly_projection, _, _ = build_display_projection(household)
    retirement_health_issues = build_retirement_health_issues(household, projection, yearly_projection)
    retirement_years = [
        {"index": index, "item": item}
        for index, item in enumerate(yearly_projection)
        if item.retirement_income > 0
    ][:10]
    first_retirement_index = next(
        (index for index, item in enumerate(yearly_projection) if item.retirement_income > 0),
        None,
    )
    retirement_gap_rows = []
    if first_retirement_index is not None:
        for index, item in enumerate(
            yearly_projection[first_retirement_index : first_retirement_index + 15],
            start=first_retirement_index,
        ):
            other_income = item.income - item.retirement_income
            draw_need = item.cash_goal_gap
            draw_percent = item.portfolio_draw_percent
            tax_summary = retirement_tax_summary(item, household)
            retirement_gap_rows.append(
                {
                    "index": index,
                    "item": item,
                    "other_income": other_income,
                    "draw_need": draw_need,
                    "draw_percent": draw_percent,
                    "risk_level": "warning" if draw_percent > Decimal("4.00") else "ok",
                    "real_cash_goal": real_value(item.annual_cash_goal, household.annual_inflation_rate, item.end_index),
                    "real_draw_need": real_value(draw_need, household.annual_inflation_rate, item.end_index),
                    **tax_summary,
                }
            )
    return render(
        request,
        "planner/retirement_detail.html",
        {
            "household": household,
            "retirement_years": retirement_years,
            "retirement_gap_rows": retirement_gap_rows,
            "retirement_health_issues": retirement_health_issues,
            **retirement_summary(household),
        },
    )


def setup_wizard(request):
    household = get_household()
    if request.method == "POST":
        form = FirstRunSetupForm(request.POST)
        if form.is_valid():
            setup_initial_data(household, form.cleaned_data)
            messages.success(request, "Setup saved. You can import accounts and depot holdings next.")
            return redirect("planner:setup")
    else:
        form = FirstRunSetupForm(initial=setup_initial_values(household))

    setup_status = {
        "people_count": household.people.count(),
        "income_rules_count": household.rules.filter(kind=MoneyRule.Kind.INCOME).count(),
        "cash_goals_count": household.cash_goals.count(),
        "accounts_count": household.accounts.count(),
        "depot_holdings_count": DepotHolding.objects.filter(asset_account__household=household).count(),
    }
    return render(
        request,
        "planner/setup.html",
        {
            "household": household,
            "form": form,
            "setup_status": setup_status,
        },
    )


def create_import_batch(household, source, filename, result, import_kind):
    status = ImportBatch.Status.FAILED if result["missing_columns"] else ImportBatch.Status.DRY_RUN
    summary = dry_run_summary(result)
    summary["import_kind"] = import_kind
    return ImportBatch.objects.create(
        household=household,
        source=source,
        status=status,
        filename=filename,
        row_count=result["row_count"],
        valid_count=result["valid_count"],
        error_count=result["error_count"],
        summary=summary,
    )


def real_data_readiness(request):
    household = get_household()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "mark_real":
            household.data_mode = Household.DataMode.REAL
            household.save(update_fields=["data_mode", "updated_at"])
            messages.success(request, "Household marked as real data.")
        return redirect("planner:real_data_readiness")

    readiness = build_household_readiness(household, request.session.get("moneymoney_diagnostics"))
    return render(
        request,
        "planner/real_data_readiness.html",
        {
            "household": household,
            **readiness,
        },
    )


@feature_required("imports")
def import_runbook(request):
    household = get_household()
    runbook = build_import_runbook(household, request.session.get("moneymoney_diagnostics"))
    return render(
        request,
        "planner/import_runbook.html",
        {
            "household": household,
            **runbook,
        },
    )


@feature_required("imports")
def moneymoney_mappings(request):
    household = get_household()
    add_form = MoneyMoneyMappingAddForm()
    if request.method == "POST":
        action = request.POST.get("action", "save_existing")
        if action == "add_mapping":
            add_form = MoneyMoneyMappingAddForm(request.POST)
            if add_form.is_valid():
                MoneyMoneyAccountMapping.objects.update_or_create(
                    household=household,
                    source_key=add_form.cleaned_data["source_key"].strip(),
                    defaults={
                        "source_kind": "manual" if not add_form.cleaned_data["source_key"].startswith("legacy-name:") else "legacy",
                        "account_name": add_form.cleaned_data["account_name"].strip(),
                        "account_type": add_form.cleaned_data["account_type"],
                        "import_enabled": add_form.cleaned_data["import_enabled"],
                        "notes": add_form.cleaned_data["notes"],
                    },
                )
                messages.success(request, "MoneyMoney mapping saved.")
                return redirect("planner:moneymoney_mappings")
        else:
            source_keys = request.POST.getlist("source_key")
            account_names = request.POST.getlist("account_name")
            source_kinds = request.POST.getlist("source_kind")
            if not source_keys and account_names:
                source_keys = [f"legacy-name:{account_name.strip()}" for account_name in account_names]
                source_kinds = ["legacy" for _account_name in account_names]
            valid_account_types = {choice for choice, _label in AssetAccount.AccountType.choices}
            saved_count = 0
            enabled_count = 0
            for index, source_key in enumerate(source_keys):
                source_key = source_key.strip()
                if not source_key:
                    continue
                account_name = account_names[index].strip() if index < len(account_names) else source_key
                source_kind = source_kinds[index].strip() if index < len(source_kinds) else ""
                account_name = account_name.strip()
                account_type = request.POST.get(f"account_type_{index}", "")
                notes = request.POST.get(f"notes_{index}", "")
                if account_type and account_type not in valid_account_types:
                    messages.error(request, f"{account_type} is not a valid LiF account type.")
                    return redirect("planner:moneymoney_mappings")
                import_enabled = request.POST.get(f"import_enabled_{index}") == "on"
                MoneyMoneyAccountMapping.objects.update_or_create(
                    household=household,
                    source_key=source_key,
                    defaults={
                        "source_kind": source_kind,
                        "account_name": account_name,
                        "account_type": account_type,
                        "import_enabled": import_enabled,
                        "notes": notes,
                    },
                )
                saved_count += 1
                if import_enabled:
                    enabled_count += 1
            messages.success(request, f"MoneyMoney selections saved. {enabled_count} enabled, {saved_count - enabled_count} disabled.")
            return redirect("planner:moneymoney_mappings")

    mapping_review = build_moneymoney_mapping_review(household)
    return render(
        request,
        "planner/moneymoney_mappings.html",
        {
            "household": household,
            "mapping_review": mapping_review,
            "add_form": add_form,
            "account_type_choices": AssetAccount.AccountType.choices,
        },
    )


@feature_required("imports")
def import_center(request):
    household = get_household()
    result = None
    batch = None
    moneymoney_diagnostics = None
    import_kind = request.POST.get("import_kind", "accounts") if request.method == "POST" else None
    if request.method == "POST":
        account_form = AccountCsvImportForm(request.POST, request.FILES) if import_kind == "accounts" else AccountCsvImportForm()
        depot_holding_form = (
            DepotHoldingCsvImportForm(request.POST, request.FILES)
            if import_kind == "depot_holdings"
            else DepotHoldingCsvImportForm()
        )
        if import_kind in {"moneymoney_accounts", "moneymoney_depot_holdings"}:
            if not feature_flag_map().get("moneymoney_import"):
                raise Http404("MoneyMoney import is not enabled.")
            try:
                connector = MoneyMoneyConnector()
                if import_kind == "moneymoney_depot_holdings":
                    disabled_source_keys = disabled_moneymoney_source_keys(household, "portfolio")
                    rows = [
                        row.as_csv_row()
                        for row in connector.depot_holding_rows()
                        if row.account_source_key not in disabled_source_keys
                    ]
                    result = depot_holding_rows_dry_run(household, rows, start_row_number=1)
                    success_message = "MoneyMoney depot holdings dry-run completed. No holdings were changed."
                else:
                    disabled_source_keys = disabled_moneymoney_source_keys(household)
                    rows = [
                        row.as_csv_row()
                        for row in connector.account_rows(account_type_overrides=moneymoney_account_type_overrides(household))
                        if row.source_key not in disabled_source_keys
                    ]
                    result = account_rows_dry_run(household, rows, start_row_number=1)
                    success_message = "MoneyMoney accounts dry-run completed. No accounts were changed."
            except MoneyMoneyConnectorUnavailable as error:
                messages.error(request, str(error))
            except Exception as error:
                messages.error(request, f"MoneyMoney import failed: {error}")
            if result is not None:
                batch = create_import_batch(
                    household=household,
                    source=ImportBatch.Source.MONEYMONEY,
                    filename=import_kind,
                    result=result,
                    import_kind=import_kind,
                )
                messages.success(request, success_message)
        elif import_kind == "moneymoney_discover_accounts":
            if not feature_flag_map().get("moneymoney_import"):
                raise Http404("MoneyMoney import is not enabled.")
            try:
                connector = MoneyMoneyConnector()
                sync_result = sync_moneymoney_mapping_rows(household, connector.account_rows())
            except MoneyMoneyConnectorUnavailable as error:
                messages.error(request, str(error))
            except Exception as error:
                messages.error(request, f"MoneyMoney discovery failed: {error}")
            else:
                messages.success(
                    request,
                    (
                        "MoneyMoney account discovery completed. "
                        f"{sync_result['synced_count']} source account(s) available for selection, "
                        f"{sync_result['created_count']} new."
                    ),
                )
        elif import_kind == "moneymoney_diagnostics":
            if not feature_flag_map().get("moneymoney_import"):
                raise Http404("MoneyMoney import is not enabled.")
            moneymoney_diagnostics = run_moneymoney_diagnostics()
            request.session["moneymoney_diagnostics"] = moneymoney_diagnostics
            if moneymoney_diagnostics["reachable"]:
                messages.success(request, "MoneyMoney diagnostics completed.")
            else:
                messages.error(request, "MoneyMoney diagnostics could not reach the connector.")
        else:
            active_form = account_form if import_kind == "accounts" else depot_holding_form
        if import_kind in {"accounts", "depot_holdings"} and active_form.is_valid():
            uploaded_file = active_form.cleaned_data["csv_file"]
            if import_kind == "depot_holdings":
                result = depot_holding_csv_dry_run(household, uploaded_file)
                source = ImportBatch.Source.CSV_DEPOT_HOLDINGS
                success_message = "Depot holdings CSV dry-run completed. No holdings were changed."
            else:
                result = account_csv_dry_run(household, uploaded_file)
                source = ImportBatch.Source.CSV_ACCOUNTS
                success_message = "CSV dry-run completed. No accounts were changed."
            batch = create_import_batch(
                household=household,
                source=source,
                filename=uploaded_file.name,
                result=result,
                import_kind=import_kind,
            )
            if batch.status == ImportBatch.Status.FAILED:
                messages.error(request, "CSV dry-run failed. Check the required columns.")
            else:
                messages.success(request, success_message)
    else:
        account_form = AccountCsvImportForm()
        depot_holding_form = DepotHoldingCsvImportForm()

    return render(
        request,
        "planner/import_center.html",
        {
            "household": household,
            "account_form": account_form,
            "depot_holding_form": depot_holding_form,
            "result": result,
            "batch": batch,
            "import_kind": import_kind,
            "moneymoney_diagnostics": moneymoney_diagnostics,
            "moneymoney_mapping_review": build_moneymoney_mapping_review(household),
            "recent_batches": [decorate_import_batch(batch) for batch in household.import_batches.all()[:10]],
            "reconciliation": build_import_reconciliation(household),
            "account_columns": ", ".join(["name", "account_type", "balance", "currency", "institution", "as_of_date"]),
            "depot_holding_columns": ", ".join(DEPOT_HOLDING_COLUMNS),
            "depot_holding_optional_columns": ", ".join(DEPOT_HOLDING_OPTIONAL_COLUMNS),
        },
    )


@feature_required("imports")
def import_batch_detail(request, pk):
    household = get_household()
    batch = get_object_or_404(ImportBatch, pk=pk, household=household)
    return render(
        request,
        "planner/import_batch_detail.html",
        {
            "household": household,
            **build_import_batch_detail(batch),
        },
    )


@feature_required("imports")
def import_batch_apply(request, pk):
    if request.method != "POST":
        raise Http404("Import batch not found.")
    household = get_household()
    batch = get_object_or_404(ImportBatch, pk=pk, household=household)
    pre_apply_snapshot = None
    if request.POST.get("create_snapshot") == "1":
        pre_apply_snapshot = create_pre_import_snapshot(household, batch)
    try:
        if batch.summary.get("import_kind") in {"depot_holdings", "moneymoney_depot_holdings"}:
            result = apply_depot_holding_import_batch(batch, pre_apply_snapshot=pre_apply_snapshot)
            item_label = "holding"
        else:
            result = apply_account_import_batch(batch, pre_apply_snapshot=pre_apply_snapshot)
            item_label = "account"
    except ValueError as error:
        if pre_apply_snapshot and batch.status != ImportBatch.Status.APPLIED:
            pre_apply_snapshot.delete()
        messages.error(request, str(error))
        return redirect("planner:import_batch_detail", pk=batch.pk)

    snapshot_message = f" Snapshot {pre_apply_snapshot.name} created first." if pre_apply_snapshot else ""
    messages.success(
        request,
        (
            f"Import applied. Created {result['created_count']} {item_label}(s), "
            f"updated {result['updated_count']} {item_label}(s).{snapshot_message}"
        ),
    )
    return redirect("planner:import_batch_detail", pk=batch.pk)


def backup_summary():
    backup_dir = Path(settings.BACKUP_DIR)
    backups = sorted(backup_dir.glob("*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True) if backup_dir.exists() else []
    latest = backups[0] if backups else None
    return {
        "backup_dir": backup_dir,
        "backup_dir_exists": backup_dir.exists(),
        "backup_count": len(backups),
        "backups": backups,
        "latest_backup": latest,
        "latest_backup_mtime": datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.get_current_timezone()) if latest else None,
    }


def database_file_summary():
    database = settings.DATABASES["default"]
    database_path = Path(str(database["NAME"]))
    exists = database_path.exists()
    return {
        "engine": database["ENGINE"],
        "path": database_path,
        "exists": exists,
        "size_bytes": database_path.stat().st_size if exists else 0,
    }


def backup_file_rows(backups):
    rows = []
    for backup in backups:
        rows.append(
            {
                "name": backup.name,
                "path": backup,
                "size_bytes": backup.stat().st_size,
                "mtime": datetime.fromtimestamp(backup.stat().st_mtime, tz=timezone.get_current_timezone()),
            }
        )
    return rows


def backup_path_for_name(filename):
    backup_dir = Path(settings.BACKUP_DIR).resolve()
    path = (backup_dir / Path(filename).name).resolve()
    if path.parent != backup_dir or path.suffix != ".sqlite3" or not path.exists():
        raise Http404("Backup not found.")
    return path


def latest_backup_matching(label):
    backup_dir = Path(settings.BACKUP_DIR)
    if not backup_dir.exists():
        return None
    matches = sorted(backup_dir.glob(f"*-{label}.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def sqlite_table_counts(path):
    table_labels = {
        "planner_household": "Households",
        "planner_person": "People",
        "planner_assetaccount": "Accounts",
        "planner_depotholding": "Depot holdings",
        "planner_moneyrule": "Rules",
        "planner_cashgoal": "Cash goals",
        "planner_debt": "Debts",
        "planner_snapshot": "Snapshots",
        "planner_importbatch": "Import batches",
    }
    try:
        with sqlite3.connect(str(path)) as db:
            db.execute("PRAGMA query_only = ON")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            valid = "planner_household" in tables and "django_migrations" in tables
            counts = []
            for table, label in table_labels.items():
                if table in tables:
                    count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    counts.append({"label": label, "count": count})
                else:
                    counts.append({"label": label, "count": None})
    except sqlite3.DatabaseError as error:
        return {"valid": False, "error": str(error), "counts": []}
    return {"valid": valid, "error": "" if valid else "This does not look like a LiF SQLite database.", "counts": counts}


def restore_sqlite_backup(backup_path):
    database = settings.DATABASES["default"]
    if database["ENGINE"] != "django.db.backends.sqlite3":
        raise CommandError("Restore currently supports SQLite only.")

    validation = sqlite_table_counts(backup_path)
    if not validation["valid"]:
        raise CommandError(validation["error"] or "Backup is not a valid LiF SQLite database.")

    target = Path(str(database["NAME"]))
    if not target.exists():
        raise CommandError(f"Database file does not exist: {target}")

    call_command("backup_data", label="pre-restore")
    pre_restore_backup = latest_backup_matching("pre-restore")
    connections.close_all()
    shutil.copy2(backup_path, target)
    connections.close_all()
    call_command("migrate", interactive=False, verbosity=0)
    call_command("check", verbosity=0)
    return pre_restore_backup


def build_real_data_readiness(database_ok, migrations_ok, staticfiles_ok, backups):
    household = active_household(create=False)
    household_ready = False
    household_detail = "No household foundation exists yet."
    quality_ready = False
    quality_detail = "Create a household foundation before checking data quality."
    if household:
        people_count = household.people.count()
        income_count = household.rules.filter(kind=MoneyRule.Kind.INCOME, is_active=True).count()
        cash_goal_count = household.cash_goals.filter(is_active=True).count()
        account_count = household.accounts.count()
        household_ready = people_count > 0 and income_count > 0 and cash_goal_count > 0
        household_detail = (
            f"{people_count} people, {income_count} active income rules, "
            f"{cash_goal_count} active cash goals, {account_count} accounts."
        )
        quality_report = build_quality_report(household)
        quality_ready = quality_report["counts"]["critical"] == 0
        quality_detail = (
            f"{quality_report['counts']['critical']} critical, "
            f"{quality_report['counts']['warning']} warning, "
            f"{quality_report['counts']['info']} info issue(s)."
        )

    missing_flags = set(FEATURE_FLAG_DEFINITIONS) - set(FeatureFlag.objects.values_list("key", flat=True))
    latest_backup = backups["latest_backup"]
    items = [
        checklist_item(
            "login",
            "Require app login",
            settings.LIF_REQUIRE_LOGIN,
            "Set LIF_REQUIRE_LOGIN=1 before entering sensitive household data.",
        ),
        checklist_item(
            "debug",
            "Disable debug mode",
            not settings.DEBUG,
            "Set DJANGO_DEBUG=0 for any always-on or VPN-accessible checkout.",
        ),
        checklist_item(
            "secret_key",
            "Use a private secret key",
            settings.SECRET_KEY != "django-insecure-local-dev-only",
            "Set DJANGO_SECRET_KEY in a private environment file outside Git.",
        ),
        checklist_item(
            "database",
            "Database reachable",
            database_ok,
            "SQLite responded to a simple health query.",
        ),
        checklist_item(
            "migrations",
            "Apply migrations",
            migrations_ok,
            "Run migrate before entering or importing real data.",
        ),
        checklist_item(
            "staticfiles",
            "Static assets available",
            staticfiles_ok,
            "The main application stylesheet can be resolved.",
        ),
        checklist_item(
            "backup_dir",
            "Configure backup directory",
            backups["backup_dir_exists"],
            f"Backup directory: {backups['backup_dir']}",
        ),
        checklist_item(
            "latest_backup",
            "Create a current backup",
            latest_backup is not None,
            f"Latest backup: {latest_backup.name}" if latest_backup else "No SQLite backup found yet.",
        ),
        checklist_item(
            "feature_flags",
            "Initialize feature flags",
            not missing_flags,
            "All feature flag rows exist." if not missing_flags else f"Missing: {', '.join(sorted(missing_flags))}.",
            "/admin/planner/featureflag/",
            "Open flags",
        ),
        checklist_item(
            "foundation",
            "Create household foundation",
            household_ready,
            household_detail,
            reverse("planner:setup"),
            "Open setup",
        ),
        checklist_item(
            "quality",
            "Review data quality",
            quality_ready,
            quality_detail,
            reverse("planner:data_quality"),
            "Open quality",
        ),
    ]
    complete_count = sum(1 for item in items if item["complete"])
    next_item = next((item for item in items if not item["complete"]), None)
    return {
        "items": items,
        "complete_count": complete_count,
        "total_count": len(items),
        "next_item": next_item,
        "is_ready": complete_count == len(items),
    }


def system_status(request):
    database_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        database_ok = False

    try:
        migrations_ok = migrations_current()
    except Exception:
        migrations_ok = False

    staticfiles_ok = bool(finders.find("planner/app.css"))
    flags = feature_flag_map()
    flag_rows = {flag.key: flag for flag in FeatureFlag.objects.all()}
    feature_flags = []
    for key, definition in FEATURE_FLAG_DEFINITIONS.items():
        override = environment_override(key)
        db_flag = flag_rows.get(key)
        feature_flags.append(
            {
                "key": key,
                "enabled": flags[key],
                "default": definition["default"],
                "description": definition["description"],
                "environment_override": override,
                "configured": db_flag is not None,
            }
        )

    warnings = []
    if settings.DEBUG:
        warnings.append("DJANGO_DEBUG is enabled.")
    if settings.SECRET_KEY == "django-insecure-local-dev-only":
        warnings.append("DJANGO_SECRET_KEY uses the local development default.")
    if not settings.LIF_REQUIRE_LOGIN:
        warnings.append("LIF_REQUIRE_LOGIN is disabled; planner data is visible without app login.")
    if not migrations_ok:
        warnings.append("There are pending migrations or migration state could not be checked.")

    backups = backup_summary()
    if not backups["backup_dir_exists"]:
        warnings.append("Backup directory does not exist yet.")
    real_data_readiness = build_real_data_readiness(database_ok, migrations_ok, staticfiles_ok, backups)

    return render(
        request,
        "planner/system_status.html",
        {
            **version_context(),
            "database_ok": database_ok,
            "migrations_ok": migrations_ok,
            "staticfiles_ok": staticfiles_ok,
            "debug": settings.DEBUG,
            "require_login": settings.LIF_REQUIRE_LOGIN,
            "allowed_hosts": settings.ALLOWED_HOSTS,
            "feature_flag_rows": feature_flags,
            "warnings": warnings,
            "real_data_readiness": real_data_readiness,
            **backups,
        },
    )


def backup_center(request):
    if request.method == "POST":
        action = request.POST.get("action", "backup")
        requested_backup_name = request.POST.get("backup_name", "")
        restore_path = None
        pre_restore_backup = None
        try:
            if action == "restore":
                if request.POST.get("confirm_restore") != "yes":
                    BackupEvent.objects.create(
                        action=BackupEvent.Action.RESTORE,
                        status=BackupEvent.Status.FAILED,
                        filename=requested_backup_name,
                        detail="Restore confirmation checkbox was not selected.",
                    )
                    raise CommandError("Confirm the restore before continuing.")
                restore_path = backup_path_for_name(requested_backup_name)
                pre_restore_backup = restore_sqlite_backup(restore_path)
                BackupEvent.objects.create(
                    action=BackupEvent.Action.RESTORE,
                    status=BackupEvent.Status.SUCCEEDED,
                    filename=restore_path.name,
                    pre_restore_filename=pre_restore_backup.name if pre_restore_backup else "",
                    detail="Restore completed. Migrations and system checks passed.",
                )
                messages.success(request, f"Restored backup {restore_path.name}.")
                return redirect("planner:system_status")
            call_command("backup_data", label="manual")
        except CommandError as error:
            if action == "restore" and request.POST.get("confirm_restore") == "yes":
                BackupEvent.objects.create(
                    action=BackupEvent.Action.RESTORE,
                    status=BackupEvent.Status.FAILED,
                    filename=restore_path.name if restore_path else requested_backup_name,
                    pre_restore_filename=pre_restore_backup.name if pre_restore_backup else "",
                    detail=str(error),
                )
            messages.error(request, str(error))
        else:
            latest_manual = latest_backup_matching("manual")
            BackupEvent.objects.create(
                action=BackupEvent.Action.BACKUP,
                status=BackupEvent.Status.SUCCEEDED,
                filename=latest_manual.name if latest_manual else "",
                detail="Manual backup created.",
            )
            messages.success(request, "Backup created.")
        return redirect("planner:backup_center")

    backups = backup_summary()
    backup_rows = backup_file_rows(backups["backups"])
    selected_name = request.GET.get("preview")
    preview = None
    if selected_name:
        preview_path = backup_path_for_name(selected_name)
        preview = {
            "name": preview_path.name,
            "size_bytes": preview_path.stat().st_size,
            "mtime": datetime.fromtimestamp(preview_path.stat().st_mtime, tz=timezone.get_current_timezone()),
            **sqlite_table_counts(preview_path),
        }

    return render(
        request,
        "planner/backup_center.html",
        {
            **version_context(),
            **backups,
            "database": database_file_summary(),
            "backup_rows": backup_rows,
            "preview": preview,
            "backup_events": BackupEvent.objects.all()[:10],
        },
    )


@feature_required("snapshots")
def snapshots(request):
    household = get_household()
    if request.method == "POST":
        form = SnapshotForm(request.POST)
        if form.is_valid():
            snapshot = form.save(commit=False)
            snapshot.household = household
            snapshot.summary = build_snapshot_summary(household)
            snapshot.save()
            messages.success(request, f"Snapshot {snapshot.name} created.")
            return redirect("planner:snapshot_detail", pk=snapshot.pk)
    else:
        has_baseline = household.baseline_snapshot is not None
        form = SnapshotForm(initial={
            "snapshot_date": timezone.localdate(),
            "name": f"Snapshot {timezone.localdate():%Y-%m-%d}",
            "snapshot_type": Snapshot.SnapshotType.BASELINE if not has_baseline else Snapshot.SnapshotType.MANUAL,
            "is_baseline": not has_baseline,
        })

    baseline_snapshot = household.baseline_snapshot
    snapshot_rows = household.snapshots.all()
    return render(
        request,
        "planner/snapshots.html",
        {
            "household": household,
            "form": form,
            "snapshots": snapshot_rows,
            "baseline_snapshot": baseline_snapshot,
        },
    )


@feature_required("snapshots")
def snapshot_detail(request, pk):
    household = get_household()
    snapshot = get_object_or_404(Snapshot, pk=pk, household=household)
    return render(
        request,
        "planner/snapshot_detail.html",
        {
            "household": household,
            "snapshot": snapshot,
            "summary": snapshot.summary,
        },
    )


@feature_required("snapshots")
def snapshot_review(request):
    household = get_household()
    snapshot_rows = list(household.snapshots.all())
    saved_reviews = household.snapshot_reviews.select_related("baseline_snapshot", "comparison_snapshot")
    baseline = None
    comparison = None
    review = None
    saved_review = None
    review_form = None
    action_form = None
    review_actions = []

    if len(snapshot_rows) >= 2:
        baseline_id = request.POST.get("baseline") if request.method == "POST" else request.GET.get("baseline")
        comparison_id = request.POST.get("comparison") if request.method == "POST" else request.GET.get("comparison")
        snapshots_by_id = {str(snapshot.pk): snapshot for snapshot in snapshot_rows}
        baseline = snapshots_by_id.get(baseline_id) or snapshot_rows[-1]
        comparison = snapshots_by_id.get(comparison_id) or snapshot_rows[0]
        if baseline.pk == comparison.pk:
            comparison = next((snapshot for snapshot in snapshot_rows if snapshot.pk != baseline.pk), None)
        if baseline and comparison:
            review = compare_snapshot_summaries(baseline.summary, comparison.summary)
            saved_review = SnapshotReview.objects.filter(
                household=household,
                baseline_snapshot=baseline,
                comparison_snapshot=comparison,
            ).first()
            if request.method == "POST":
                action = request.POST.get("action", "save_review")
                if action == "save_review":
                    review_form = SnapshotReviewForm(request.POST, instance=saved_review)
                    if review_form.is_valid():
                        saved_review = review_form.save(commit=False)
                        saved_review.household = household
                        saved_review.baseline_snapshot = baseline
                        saved_review.comparison_snapshot = comparison
                        saved_review.save()
                        messages.success(request, f"Annual review {saved_review.title} saved.")
                        return redirect(
                            f"{reverse('planner:snapshot_review')}?baseline={baseline.pk}&comparison={comparison.pk}"
                        )
                elif action == "add_review_action" and saved_review:
                    action_form = SnapshotReviewActionForm(request.POST, household=household)
                    if action_form.is_valid():
                        review_action = action_form.save(commit=False)
                        review_action.review = saved_review
                        review_action.save()
                        messages.success(request, f"Review action {review_action.title} added.")
                        return redirect(
                            f"{reverse('planner:snapshot_review')}?baseline={baseline.pk}&comparison={comparison.pk}"
                        )
                elif action == "add_review_action":
                    messages.error(request, "Save the annual review before adding actions.")
                elif action == "update_review_action" and saved_review:
                    review_action = get_object_or_404(
                        SnapshotReviewAction,
                        pk=request.POST.get("review_action_id"),
                        review=saved_review,
                    )
                    status = request.POST.get("status")
                    if status in SnapshotReviewAction.Status.values:
                        review_action.status = status
                        review_action.save(update_fields=["status", "updated_at"])
                        messages.success(request, f"Review action {review_action.title} updated.")
                    return redirect(
                        f"{reverse('planner:snapshot_review')}?baseline={baseline.pk}&comparison={comparison.pk}"
                    )
                if review_form is None:
                    review_form = SnapshotReviewForm(instance=saved_review)
                if action_form is None:
                    action_form = SnapshotReviewActionForm(household=household)
                if saved_review:
                    review_actions = saved_review.actions.select_related("owner")
            else:
                initial = {}
                if saved_review is None:
                    initial = {
                        "title": f"{baseline.snapshot_date:%Y} review",
                        "review_date": timezone.localdate(),
                    }
                review_form = SnapshotReviewForm(instance=saved_review, initial=initial)
                action_form = SnapshotReviewActionForm(household=household)
                if saved_review:
                    review_actions = saved_review.actions.select_related("owner")

    return render(
        request,
        "planner/snapshot_review.html",
        {
            "household": household,
            "snapshots": snapshot_rows,
            "baseline": baseline,
            "comparison": comparison,
            "review": review,
            "saved_review": saved_review,
            "saved_reviews": saved_reviews,
            "review_form": review_form,
            "action_form": action_form,
            "review_actions": review_actions,
        },
    )


@feature_required("snapshots")
def snapshot_compare(request, pk):
    household = get_household()
    snapshot = get_object_or_404(Snapshot, pk=pk, household=household)
    current_summary = build_snapshot_summary(household)
    comparison = compare_snapshot_to_current(snapshot.summary, current_summary)
    return render(
        request,
        "planner/snapshot_compare.html",
        {
            "household": household,
            "snapshot": snapshot,
            "comparison": comparison,
        },
    )


@feature_required("snapshots")
def snapshot_projection_changes(request):
    household = get_household()
    snapshot = household.snapshots.first()
    comparison = None
    drivers = None
    if snapshot:
        current_summary = build_snapshot_summary(household)
        comparison = compare_projection_summaries(snapshot.summary, current_summary)
        drivers = build_projection_change_drivers(snapshot.summary, current_summary)
    return render(
        request,
        "planner/snapshot_projection_changes.html",
        {
            "household": household,
            "snapshot": snapshot,
            "comparison": comparison,
            "drivers": drivers,
        },
    )


@feature_required("analytics")
def analytics(request):
    household = get_household()
    projection = build_projection(household)
    yearly_projection = build_yearly_projection(projection, household.cash_goals.all())
    analytics_data = build_analytics_data(projection, yearly_projection, household)
    include_sequence_risk = request.GET.get("sequence_risk") == "1"
    sequence_risk = build_sequence_risk_summary(household) if include_sequence_risk else None
    return render(
        request,
        "planner/analytics.html",
        {
            "household": household,
            "analytics_data": analytics_data,
            "sequence_risk": sequence_risk,
            "default_granularity": household.resolved_display_granularity,
        },
    )


def income_timeline(request):
    household = get_household()
    timeline = build_income_timeline(household)
    selected_source = request.GET.get("source")
    selected_index = timeline["columns"].index(selected_source) if selected_source in timeline["columns"] else None
    return render(
        request,
        "planner/income_timeline.html",
        {
            "household": household,
            "timeline": timeline,
            "selected_source": selected_source if selected_index is not None else None,
            "selected_index": selected_index,
        },
    )


def _assumption_review_targets(registry, scope, review_key="", group_key=""):
    if scope == "group":
        group = next((item for item in registry["groups"] if item["key"] == group_key), None)
        if not group:
            raise Http404("Assumption group not found.")
        return [(row["review_key"], row["label"]) for row in group["rows"]]

    for group in registry["groups"]:
        row = next((item for item in group["rows"] if item["review_key"] == review_key), None)
        if row:
            return [(row["review_key"], row["label"])]
    raise Http404("Assumption not found.")


def _assumption_review_context(registry, scope, review_key="", group_key=""):
    if scope == "group":
        group = next((item for item in registry["groups"] if item["key"] == group_key), None)
        if not group:
            raise Http404("Assumption group not found.")
        return {
            "scope": "group",
            "group": group,
            "title": group["label"],
            "subtitle": f"{len(group['rows'])} assumptions in this group.",
            "targets": [(row["review_key"], row["label"]) for row in group["rows"]],
        }

    for group in registry["groups"]:
        row = next((item for item in group["rows"] if item["review_key"] == review_key), None)
        if row:
            return {
                "scope": "row",
                "group": group,
                "row": row,
                "title": row["label"],
                "subtitle": group["label"],
                "targets": [(row["review_key"], row["label"])],
            }
    raise Http404("Assumption not found.")


def _mark_assumption_reviews(household, review_targets, reviewed_by="", note=""):
    for key, label in review_targets:
        AssumptionReview.objects.update_or_create(
            household=household,
            key=key,
            defaults={"label": label, "reviewed_by": reviewed_by, "note": note},
        )


def assumptions_registry(request):
    household = get_household()
    reviews = list(household.assumption_reviews.all())
    registry = build_assumption_registry(household, reviews=reviews)
    if request.method == "POST":
        scope = request.POST.get("scope", "row")
        reviewed_by = request.POST.get("reviewed_by", "").strip()
        note = request.POST.get("note", "").strip()
        review_targets = _assumption_review_targets(
            registry,
            scope,
            review_key=request.POST.get("review_key", ""),
            group_key=request.POST.get("group_key", ""),
        )
        _mark_assumption_reviews(household, review_targets, reviewed_by, note)
        messages.success(
            request,
            f"Marked {len(review_targets)} assumption review(s) current.",
        )
        return redirect("planner:assumptions_registry")
    return render(
        request,
        "planner/assumptions_registry.html",
        {
            "household": household,
            "registry": registry,
        },
    )


def assumption_review_center(request):
    household = get_household()
    registry = build_assumption_registry(household, reviews=list(household.assumption_reviews.all()))
    review_center = build_assumption_review_center(registry)
    return render(
        request,
        "planner/assumption_review_center.html",
        {
            "household": household,
            "registry": registry,
            "review_center": review_center,
        },
    )


def assumption_review_edit(request):
    household = get_household()
    reviews = list(household.assumption_reviews.all())
    registry = build_assumption_registry(household, reviews=reviews)
    if request.method == "POST":
        scope = request.POST.get("scope", "row")
        review_key = request.POST.get("review_key", "")
        group_key = request.POST.get("group_key", "")
        review_targets = _assumption_review_targets(
            registry,
            scope,
            review_key=review_key,
            group_key=group_key,
        )
        _mark_assumption_reviews(
            household,
            review_targets,
            request.POST.get("reviewed_by", "").strip(),
            request.POST.get("note", "").strip(),
        )
        messages.success(
            request,
            f"Marked {len(review_targets)} assumption review(s) current.",
        )
        return redirect("planner:assumptions_registry")

    scope = request.GET.get("scope", "row")
    review_context = _assumption_review_context(
        registry,
        scope,
        review_key=request.GET.get("review_key", ""),
        group_key=request.GET.get("group_key", ""),
    )
    target_keys = [key for key, _label in review_context["targets"]]
    existing_reviews = {
        review.key: review
        for review in household.assumption_reviews.filter(key__in=target_keys)
    }
    existing_review = None
    if len(existing_reviews) == 1:
        existing_review = next(iter(existing_reviews.values()))
    return render(
        request,
        "planner/assumption_review_form.html",
        {
            "household": household,
            "review_context": review_context,
            "existing_review": existing_review,
        },
    )


def projection_audit(request, month_index):
    household = get_household()
    display_mode = forecast_display_mode(request)
    projection = build_projection(household)
    if not projection:
        raise Http404("No projection months available.")

    # Optional "jump to month" picker: ?goto=YYYY-MM overrides the path index.
    goto = request.GET.get("goto")
    if goto:
        try:
            target_year, target_month = (int(part) for part in goto.split("-")[:2])
            for position, item in enumerate(projection):
                if item.month.year == target_year and item.month.month == target_month:
                    month_index = position
                    break
        except (ValueError, AttributeError):
            pass

    if month_index < 0 or month_index >= len(projection):
        raise Http404("Projection month not found.")

    month = projection[month_index]
    groups = grouped_audit_lines(month.audit_lines)
    account_balances = [
        {"account": account, "balance": month.account_balances.get(account.id)}
        for account in household.accounts.all()
        if account.id in month.account_balances
    ]
    return render(
        request,
        "planner/projection_audit.html",
        {
            "household": household,
            "month": month,
            "groups": groups,
            "driver_summary": forecast_driver_summary(month),
            "explanation": forecast_explanation_summary(month, groups),
            "warnings": forecast_warnings(month, household),
            "account_balances": account_balances,
            "display_mode": display_mode,
            "horizon_start": projection[0].month,
            "horizon_end": projection[-1].month,
            **projection_navigation(month_index, len(projection)),
        },
    )


def projection_year_audit(request, year_index):
    household = get_household()
    display_mode = forecast_display_mode(request)
    projection = build_projection(household)
    yearly_projection = build_yearly_projection(projection, household.cash_goals.all())
    if not yearly_projection:
        raise Http404("No projection years available.")

    # Optional "jump to year" picker: ?goto_year=YYYY overrides the path index.
    goto_year = request.GET.get("goto_year")
    if goto_year:
        try:
            target = int(goto_year)
            for position, item in enumerate(yearly_projection):
                if item.year == target:
                    year_index = position
                    break
        except ValueError:
            pass

    if year_index < 0 or year_index >= len(yearly_projection):
        raise Http404("Projection year not found.")

    year = yearly_projection[year_index]
    groups = grouped_audit_lines(year.audit_lines)
    months = projection[year.start_index : year.end_index + 1]
    # Per-account balances as of year-end (the last month in the bucket).
    end_month = projection[year.end_index]
    account_balances = [
        {"account": account, "balance": end_month.account_balances.get(account.id)}
        for account in household.accounts.all()
        if account.id in end_month.account_balances
    ]
    return render(
        request,
        "planner/projection_year_audit.html",
        {
            "household": household,
            "year": year,
            "months": months,
            "groups": groups,
            "driver_summary": forecast_driver_summary(year),
            "explanation": forecast_explanation_summary(year, groups),
            "warnings": forecast_warnings(year, household, months),
            "account_balances": account_balances,
            "display_mode": display_mode,
            "year_options": [{"value": item.year, "label": item.label} for item in yearly_projection],
            **projection_navigation(year_index, len(yearly_projection)),
        },
    )


def household_settings(request):
    household = get_household()
    if request.method == "POST":
        form = HouseholdForm(request.POST, instance=household)
        if form.is_valid():
            changed_assumptions = sorted(set(form.changed_data) & HOUSEHOLD_ASSUMPTION_FIELDS)
            form.save()
            messages.success(request, "Household settings saved.")
            if changed_assumptions:
                messages.warning(
                    request,
                    (
                        "Long-range assumptions changed: "
                        f"{', '.join(changed_assumptions)}. "
                        f"Consider creating a snapshot before comparing forecast results: {reverse('planner:snapshots')}"
                    ),
                )
            return redirect("planner:dashboard")
    else:
        form = HouseholdForm(instance=household)

    households = Household.objects.order_by("-is_active", "name", "pk")
    return render(
        request,
        "planner/household_settings.html",
        {
            "household": household,
            "form": form,
            "people": household.people.all(),
            "households": households,
            "household_count": Household.objects.count(),
        },
    )


def household_switch(request, pk):
    if request.method != "POST":
        raise Http404("Household switch requires POST.")
    household = get_object_or_404(Household, pk=pk)
    household.is_active = True
    household.save(update_fields=["is_active"])
    messages.success(request, f"Switched to {household.name}.")
    return redirect(request.POST.get("next") or "planner:dashboard")


def _clone_household_and_notify(request, source, name):
    clone = clone_household(source, name=name, make_active=True)
    messages.success(
        request,
        (
            f"Cloned {source.name} to {clone.name}. You are now editing the clone. "
            "Imports, snapshots, annual reviews, and connector mappings were not copied."
        ),
    )
    return clone


def household_clone(request, pk):
    if request.method != "POST":
        raise Http404("Household clone requires POST.")
    source = get_object_or_404(Household, pk=pk)
    name = (request.POST.get("name") or "").strip() or f"{source.name} copy"
    _clone_household_and_notify(request, source, name)
    return redirect("planner:household_settings")


def household_delete(request, pk):
    if request.method != "POST":
        raise Http404("Household delete requires POST.")
    household = get_object_or_404(Household, pk=pk)
    if Household.objects.count() <= 1:
        messages.error(request, "Keep at least one household.")
        return redirect("planner:household_settings")
    if household.is_active:
        messages.error(request, "Switch to another household before deleting this one.")
        return redirect("planner:household_settings")
    name = household.name
    household.delete()
    messages.success(request, f"{name} deleted.")
    return redirect("planner:household_settings")


def person_create(request):
    household = get_household()
    if request.method == "POST":
        form = PersonForm(request.POST)
        if form.is_valid():
            person = form.save(commit=False)
            person.household = household
            person.save()
            messages.success(request, f"{person.name} added.")
            return redirect("planner:household_settings")
    else:
        form = PersonForm()
    return render(request, "planner/form.html", {"title": "Add person", "form": form})


def person_update(request, pk):
    household = get_household()
    person = get_object_or_404(Person, pk=pk, household=household)
    if request.method == "POST":
        form = PersonForm(request.POST, instance=person)
        if form.is_valid():
            form.save()
            messages.success(request, f"{person.name} updated.")
            return redirect("planner:household_settings")
    else:
        form = PersonForm(instance=person)
    return render(request, "planner/form.html", {"title": f"Edit {person.name}", "form": form})


def rule_create(request):
    household = get_household()
    if request.method == "POST":
        form = MoneyRuleForm(request.POST, household=household)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.household = household
            rule.save()
            messages.success(request, f"{rule.name} added.")
            return redirect("planner:plan_index")
    else:
        form = MoneyRuleForm(household=household)
    return render(request, "planner/form.html", {"title": "Add money rule", "form": form})


def rule_update(request, pk):
    household = get_household()
    rule = get_object_or_404(MoneyRule, pk=pk, household=household)
    if request.method == "POST":
        form = MoneyRuleForm(request.POST, instance=rule, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{rule.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = MoneyRuleForm(instance=rule, household=household)
    return render(request, "planner/form.html", {"title": f"Edit {rule.name}", "form": form})


def transfer_rule_create(request):
    household = get_household()
    if request.method == "POST":
        form = TransferRuleForm(request.POST, household=household)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.household = household
            rule.save()
            messages.success(request, f"{rule.name} added.")
            return redirect("planner:plan_index")
    else:
        form = TransferRuleForm(household=household)
    return render(request, "planner/form.html", {"title": "Add transfer rule", "form": form})


def transfer_rule_update(request, pk):
    household = get_household()
    rule = get_object_or_404(TransferRule, pk=pk, household=household)
    if request.method == "POST":
        form = TransferRuleForm(request.POST, instance=rule, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{rule.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = TransferRuleForm(instance=rule, household=household)
    return render(request, "planner/form.html", {"title": f"Edit {rule.name}", "form": form})


def family_gift_plan_create(request):
    household = get_household()
    if request.method == "POST":
        form = FamilyGiftPlanForm(request.POST, household=household)
        if form.is_valid():
            gift = form.save(commit=False)
            gift.household = household
            gift.save()
            messages.success(request, f"{gift.name} added.")
            return redirect("planner:plan_index")
    else:
        form = FamilyGiftPlanForm(household=household)
    return render(
        request,
        "planner/form.html",
        {
            "title": "Add family gift",
            "form": form,
            "form_hint": "Use this for planned gifts to children, such as funding a Kinderdepot. The target account should be tracked outside household net worth.",
        },
    )


def family_gift_plan_update(request, pk):
    household = get_household()
    gift = get_object_or_404(FamilyGiftPlan, pk=pk, household=household)
    if request.method == "POST":
        form = FamilyGiftPlanForm(request.POST, instance=gift, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{gift.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = FamilyGiftPlanForm(instance=gift, household=household)
    return render(
        request,
        "planner/form.html",
        {
            "title": f"Edit {gift.name}",
            "form": form,
            "form_hint": "Use this for planned gifts to children, such as funding a Kinderdepot. The target account should be tracked outside household net worth.",
        },
    )


def planned_investment_purchase_create(request):
    household = get_household()
    if request.method == "POST":
        form = PlannedInvestmentPurchaseForm(request.POST, household=household)
        if form.is_valid():
            purchase = form.save(commit=False)
            purchase.household = household
            purchase.save()
            messages.success(request, f"{purchase.name} added.")
            return redirect("planner:plan_index")
    else:
        form = PlannedInvestmentPurchaseForm(household=household)
    return render(
        request,
        "planner/form.html",
        {
            "title": "Add planned investment purchase",
            "form": form,
            "form_hint": (
                "Use this for a future ETF, stock, or bond purchase. The forecast moves cash from the funding "
                "account into the depot on the purchase month; optional payout fields model a later bond maturity."
            ),
        },
    )


def planned_investment_purchase_update(request, pk):
    household = get_household()
    purchase = get_object_or_404(PlannedInvestmentPurchase, pk=pk, household=household)
    if request.method == "POST":
        form = PlannedInvestmentPurchaseForm(request.POST, instance=purchase, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{purchase.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = PlannedInvestmentPurchaseForm(instance=purchase, household=household)
    return render(
        request,
        "planner/form.html",
        {
            "title": f"Edit {purchase.name}",
            "form": form,
            "form_hint": (
                "Use payout date and payout amount for bonds that should mature into cash later in the forecast."
            ),
        },
    )


def account_create(request):
    household = get_household()
    if request.method == "POST":
        form = AssetAccountForm(request.POST, household=household)
        if form.is_valid():
            account = form.save(commit=False)
            account.household = household
            account.save()
            messages.success(request, f"{account.name} added.")
            return redirect("planner:account_index")
    else:
        form = AssetAccountForm(initial={"currency": household.currency}, household=household)
    return render(request, "planner/form.html", {"title": "Add account", "form": form})


def account_setup(request):
    household = get_household()
    if request.method == "POST":
        form = AccountSetupWizardForm(request.POST, household=household)
        if form.is_valid():
            account, holding, debt = create_account_from_setup(household, form.cleaned_data)
            extra = ""
            if holding:
                extra = f" First holding {holding.name} added."
            if debt:
                extra = f" Debt plan {debt.name} added."
            messages.success(request, f"{account.name} added.{extra}")
            return redirect("planner:account_detail", pk=account.pk)
    else:
        form = AccountSetupWizardForm(initial=account_setup_initial(household), household=household)
    return render(
        request,
        "planner/account_setup.html",
        {
            "household": household,
            "form": form,
            "account_types": AssetAccount.AccountType,
        },
    )


def account_update(request, pk):
    household = get_household()
    account = get_object_or_404(AssetAccount, pk=pk, household=household)
    if request.method == "POST":
        form = AssetAccountForm(request.POST, instance=account, household=household)
        if form.is_valid():
            account = form.save()
            # Switching to holdings valuation makes the holdings authoritative.
            account.sync_balance_from_holdings()
            messages.success(request, f"{account.name} updated.")
            return redirect("planner:account_detail", pk=account.pk)
    else:
        form = AssetAccountForm(instance=account, household=household)
    return render(request, "planner/form.html", {"title": f"Edit {account.name}", "form": form})


@feature_required("cash_goals")
def cash_goal_create(request):
    household = get_household()
    if request.method == "POST":
        form = CashGoalForm(request.POST)
        if form.is_valid():
            cash_goal = form.save(commit=False)
            cash_goal.household = household
            cash_goal.save()
            messages.success(request, f"{cash_goal.name} added.")
            return redirect("planner:cash_goal_index")
    else:
        form = CashGoalForm(initial={"start_year": household.start_month.year, "annual_amount": Decimal("30000.00")})
    return render(request, "planner/form.html", {"title": "Add cash goal", "form": form})


@feature_required("cash_goals")
def cash_goal_update(request, pk):
    household = get_household()
    cash_goal = get_object_or_404(CashGoal, pk=pk, household=household)
    if request.method == "POST":
        form = CashGoalForm(request.POST, instance=cash_goal)
        if form.is_valid():
            form.save()
            messages.success(request, f"{cash_goal.name} updated.")
            return redirect("planner:cash_goal_index")
    else:
        form = CashGoalForm(instance=cash_goal)
    return render(request, "planner/form.html", {"title": f"Edit {cash_goal.name}", "form": form})


@feature_required("depot_holdings")
def depot_holding_create(request):
    household = get_household()
    if request.method == "POST":
        form = DepotHoldingForm(request.POST, household=household)
        if form.is_valid():
            holding = form.save()
            messages.success(request, f"{holding.name} added.")
            return redirect("planner:holding_index")
    else:
        initial = {"currency": household.currency}
        depot_accounts = household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT)
        requested = request.GET.get("account")
        depot_account = depot_accounts.filter(pk=requested).first() if requested else None
        if depot_account is None:
            depot_account = depot_accounts.first()
        if depot_account:
            initial["asset_account"] = depot_account
        form = DepotHoldingForm(household=household, initial=initial)
    return render(
        request,
        "planner/form.html",
        {"title": "Add depot holding", "form": form, "form_hint": DEPOT_HOLDING_HINT},
    )


@feature_required("depot_holdings")
def depot_holding_update(request, pk):
    household = get_household()
    holding = get_object_or_404(DepotHolding, pk=pk, asset_account__household=household)
    if request.method == "POST":
        form = DepotHoldingForm(request.POST, instance=holding, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{holding.name} updated.")
            return redirect("planner:holding_index")
    else:
        form = DepotHoldingForm(instance=holding, household=household)
    return render(
        request,
        "planner/form.html",
        {"title": f"Edit {holding.name}", "form": form, "form_hint": DEPOT_HOLDING_HINT},
    )


@feature_required("debts")
def debt_create(request):
    household = get_household()
    if request.method == "POST":
        form = DebtForm(request.POST, household=household)
        if form.is_valid():
            debt = form.save(commit=False)
            debt.household = household
            debt.save()  # Debt.save() syncs the linked loan account balance.
            messages.success(request, f"{debt.name} added.")
            return redirect("planner:debt_index")
    else:
        initial = {}
        requested = request.GET.get("account")
        if requested:
            loan_account = household.accounts.filter(
                account_type=AssetAccount.AccountType.LOAN, pk=requested
            ).first()
            if loan_account:
                initial["account"] = loan_account
        form = DebtForm(household=household, initial=initial)
    return render(request, "planner/debt_form.html", {"title": "Add debt", "form": form})


@feature_required("debts")
def debt_update(request, pk):
    household = get_household()
    debt = get_object_or_404(Debt, pk=pk, household=household)
    if request.method == "POST":
        form = DebtForm(request.POST, instance=debt, household=household)
        if form.is_valid():
            debt = form.save()  # Debt.save() syncs the linked loan account balance.
            messages.success(request, f"{debt.name} updated.")
            return redirect("planner:debt_index")
    else:
        form = DebtForm(instance=debt, household=household)
    return render(request, "planner/debt_form.html", {"title": f"Edit {debt.name}", "form": form})


@feature_required("debts")
def debt_detail(request, pk):
    household = get_household()
    debt = get_object_or_404(Debt.objects.select_related("account", "source_account"), pk=pk, household=household)
    start = first_of_month(household.start_month)
    # Cap displayed rows so a non-amortizing loan cannot render thousands.
    display_cap = min(MAX_AMORTIZATION_MONTHS, 600)
    schedule = list(iter_debt_schedule(debt, start, max_months=display_cap))
    summary = summarize_debt(debt, start)
    return render(
        request,
        "planner/debt_detail.html",
        {
            "household": household,
            "debt": debt,
            "schedule": schedule,
            "summary": summary,
            "schedule_truncated": summary["payoff_month"] is None and len(schedule) >= display_cap,
        },
    )


@feature_required("income_investments")
def income_investment_create(request):
    household = get_household()
    if request.method == "POST":
        form = IncomeInvestmentForm(request.POST, household=household)
        if form.is_valid():
            investment = form.save(commit=False)
            investment.household = household
            investment.save()
            messages.success(request, f"{investment.name} added.")
            return redirect("planner:plan_index")
    else:
        form = IncomeInvestmentForm(initial={"currency": household.currency}, household=household)
    return render(
        request,
        "planner/form.html",
        {"title": "Add income investment", "form": form, "form_hint": INCOME_INVESTMENT_HINT},
    )


@feature_required("income_investments")
def income_investment_update(request, pk):
    household = get_household()
    investment = get_object_or_404(IncomeInvestment, pk=pk, household=household)
    if request.method == "POST":
        form = IncomeInvestmentForm(request.POST, instance=investment, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{investment.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = IncomeInvestmentForm(instance=investment, household=household)
    return render(
        request,
        "planner/form.html",
        {"title": f"Edit {investment.name}", "form": form, "form_hint": INCOME_INVESTMENT_HINT},
    )


@feature_required("income_investments")
def private_loan_create(request):
    household = get_household()
    if request.method == "POST":
        form = PrivateLoanReceivableForm(request.POST, household=household)
        if form.is_valid():
            loan = form.save(commit=False)
            loan.household = household
            loan.save()
            messages.success(request, f"{loan.name} added.")
            return redirect("planner:plan_index")
    else:
        form = PrivateLoanReceivableForm(household=household)
    return render(
        request,
        "planner/form.html",
        {
            "title": "Add private loan receivable",
            "form": form,
            "form_hint": "Use this when someone owes you money. Zins is income. Tilgung moves value from the receivable into the repayment account. If Tilgung is 0, the remaining principal is paid back in the final repayment month.",
        },
    )


@feature_required("income_investments")
def private_loan_update(request, pk):
    household = get_household()
    loan = get_object_or_404(PrivateLoanReceivable, pk=pk, household=household)
    if request.method == "POST":
        form = PrivateLoanReceivableForm(request.POST, instance=loan, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{loan.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = PrivateLoanReceivableForm(instance=loan, household=household)
    return render(
        request,
        "planner/form.html",
        {
            "title": f"Edit {loan.name}",
            "form": form,
            "form_hint": "Zins counts as income. Tilgung is not income; it moves value from the receivable asset into the repayment account. The final repayment month clears whatever principal remains.",
        },
    )


@feature_required("real_estate")
def real_estate_create(request):
    household = get_household()
    if request.method == "POST":
        form = RealEstateForm(request.POST, household=household)
        if form.is_valid():
            property_item = form.save(commit=False)
            property_item.household = household
            property_item.save()
            form.save_m2m()  # links the selected debts to this property
            messages.success(request, f"{property_item.name} added.")
            return redirect("planner:real_estate_index")
    else:
        form = RealEstateForm(initial={"currency": household.currency}, household=household)
    return render(request, "planner/form.html", {"title": "Add property", "form": form})


@feature_required("real_estate")
def real_estate_update(request, pk):
    household = get_household()
    property_item = get_object_or_404(RealEstate, pk=pk, household=household)
    if request.method == "POST":
        form = RealEstateForm(request.POST, instance=property_item, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{property_item.name} updated.")
            return redirect("planner:real_estate_index")
    else:
        form = RealEstateForm(instance=property_item, household=household)
    return render(request, "planner/form.html", {"title": f"Edit {property_item.name}", "form": form})


@feature_required("real_estate")
def real_estate_transfer_plan_create(request):
    household = get_household()
    if request.method == "POST":
        form = RealEstateTransferPlanForm(request.POST, household=household)
        if form.is_valid():
            plan = form.save(commit=False)
            plan.household = household
            plan.save()
            messages.success(request, f"{plan.name} added.")
            return redirect("planner:real_estate_index")
    else:
        form = RealEstateTransferPlanForm(household=household)
    return render(
        request,
        "planner/form.html",
        {
            "title": "Add property transfer",
            "form": form,
            "form_hint": "Use this for gifting real estate, including a retained Nießbrauch. Enter the taxable gift value explicitly; LiF does not calculate legal valuation.",
        },
    )


@feature_required("real_estate")
def real_estate_transfer_plan_update(request, pk):
    household = get_household()
    plan = get_object_or_404(RealEstateTransferPlan, pk=pk, household=household)
    if request.method == "POST":
        form = RealEstateTransferPlanForm(request.POST, instance=plan, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{plan.name} updated.")
            return redirect("planner:real_estate_index")
    else:
        form = RealEstateTransferPlanForm(instance=plan, household=household)
    return render(
        request,
        "planner/form.html",
        {
            "title": f"Edit {plan.name}",
            "form": form,
            "form_hint": "Use this for gifting real estate, including a retained Nießbrauch. Enter the taxable gift value explicitly; LiF does not calculate legal valuation.",
        },
    )


@feature_required("retirement_plans")
def retirement_plan_create(request):
    household = get_household()
    if request.method == "POST":
        form = RetirementPlanForm(request.POST, household=household)
        if form.is_valid():
            plan = form.save(commit=False)
            plan.household = household
            plan.save()
            messages.success(request, f"{plan.name} added.")
            return redirect("planner:retirement_plan_index")
    else:
        form = RetirementPlanForm(household=household)
    return render(request, "planner/form.html", {"title": "Add retirement plan", "form": form})


@feature_required("retirement_plans")
def retirement_plan_update(request, pk):
    household = get_household()
    plan = get_object_or_404(RetirementPlan, pk=pk, household=household)
    if request.method == "POST":
        form = RetirementPlanForm(request.POST, instance=plan, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{plan.name} updated.")
            return redirect("planner:retirement_plan_index")
    else:
        form = RetirementPlanForm(instance=plan, household=household)
    return render(request, "planner/form.html", {"title": f"Edit {plan.name}", "form": form})


@feature_required("equity_grants")
def equity_grant_create(request):
    household = get_household()
    if request.method == "POST":
        form = EquityGrantForm(request.POST, household=household)
        if form.is_valid():
            grant = form.save(commit=False)
            grant.household = household
            grant.save()
            messages.success(request, f"{grant.name} added.")
            return redirect("planner:plan_index")
    else:
        form = EquityGrantForm(household=household, initial={"currency": household.currency})
    return render(
        request,
        "planner/form.html",
        {"title": "Add RSU / equity grant", "form": form, "form_hint": EQUITY_GRANT_HINT},
    )


@feature_required("equity_grants")
def equity_grant_update(request, pk):
    household = get_household()
    grant = get_object_or_404(EquityGrant, pk=pk, household=household)
    if request.method == "POST":
        form = EquityGrantForm(request.POST, instance=grant, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{grant.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = EquityGrantForm(instance=grant, household=household)
    return render(
        request,
        "planner/form.html",
        {"title": f"Edit {grant.name}", "form": form, "form_hint": EQUITY_GRANT_HINT},
    )


@feature_required("analytics")
def goal_planner(request):
    household = get_household()
    form = GoalPlannerForm(request.GET or None, household=household)
    result = None
    target_month = None
    if form.is_valid():
        target_month = date(form.cleaned_data["target_year"], 12, 1)
        result = solve_monthly_contribution(
            household,
            target_net_worth=form.cleaned_data["target_net_worth"],
            target_month=target_month,
            start_month=form.cleaned_data.get("start_month") or household.start_month,
            target_account=form.cleaned_data.get("target_account"),
        )
    return render(
        request,
        "planner/goal_planner.html",
        {
            "household": household,
            "form": form,
            "result": result,
            "target_month": target_month,
        },
    )


@feature_required("scenarios")
def scenario_compare(request):
    household = get_household()
    comparison = build_scenario_comparison(household)
    projection = build_projection(household)
    yearly_projection = build_yearly_projection(projection, household.cash_goals.all())
    scenario_data_confidence = account_data_confidence(account_reconciliation_rows(household))
    return render(
        request,
        "planner/scenario_compare.html",
        {
            "household": household,
            "comparison": comparison,
            "data_confidence": scenario_data_confidence,
            "sensitivity_groups": build_assumption_sensitivity(household),
            "active_scenario_count": household.scenarios.filter(is_active=True).count(),
            "retirement_health_issues": build_retirement_health_issues(household, projection, yearly_projection),
        },
    )


@feature_required("scenarios")
def scenario_household_clone(request):
    household = get_household()
    initial_name = f"Scenario: {household.name}"
    if request.method == "POST":
        form = ScenarioHouseholdCloneForm(request.POST)
        if form.is_valid():
            _clone_household_and_notify(request, household, form.cleaned_data["name"])
            return redirect("planner:dashboard")
    else:
        form = ScenarioHouseholdCloneForm(initial={"name": initial_name})
    return render(
        request,
        "planner/scenario_household_clone.html",
        {
            "household": household,
            "form": form,
        },
    )


@feature_required("scenarios")
def scenario_create(request):
    household = get_household()
    if request.method == "POST":
        form = ScenarioForm(request.POST)
        if form.is_valid():
            scenario = form.save(commit=False)
            scenario.household = household
            scenario.save()
            messages.success(request, f"{scenario.name} added.")
            return redirect("planner:scenario_compare")
    else:
        form = ScenarioForm()
    return render(request, "planner/form.html", {"title": "Add scenario", "form": form})


@feature_required("scenarios")
def scenario_update(request, pk):
    household = get_household()
    scenario = get_object_or_404(Scenario, pk=pk, household=household)
    if request.method == "POST":
        form = ScenarioForm(request.POST, instance=scenario)
        if form.is_valid():
            form.save()
            messages.success(request, f"{scenario.name} updated.")
            return redirect("planner:scenario_compare")
    else:
        form = ScenarioForm(instance=scenario)
    return render(request, "planner/form.html", {"title": f"Edit {scenario.name}", "form": form})


@feature_required("true_expenses")
def true_expense_create(request):
    household = get_household()
    if request.method == "POST":
        form = TrueExpenseForm(request.POST)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.household = household
            expense.save()
            messages.success(request, f"{expense.name} added.")
            return redirect("planner:plan_index")
    else:
        form = TrueExpenseForm()
    return render(request, "planner/form.html", {"title": "Add true expense", "form": form})


@feature_required("true_expenses")
def true_expense_update(request, pk):
    household = get_household()
    expense = get_object_or_404(TrueExpense, pk=pk, household=household)
    if request.method == "POST":
        form = TrueExpenseForm(request.POST, instance=expense)
        if form.is_valid():
            form.save()
            messages.success(request, f"{expense.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = TrueExpenseForm(instance=expense)
    return render(request, "planner/form.html", {"title": f"Edit {expense.name}", "form": form})


@feature_required("child_milestones")
def child_milestone_create(request):
    household = get_household()
    if request.method == "POST":
        form = ChildMilestoneForm(request.POST, household=household)
        if form.is_valid():
            milestone = form.save()
            messages.success(request, f"{milestone.name} added.")
            return redirect("planner:plan_index")
    else:
        form = ChildMilestoneForm(household=household)
    return render(request, "planner/form.html", {"title": "Add child milestone", "form": form})


@feature_required("child_milestones")
def child_milestone_update(request, pk):
    household = get_household()
    milestone = get_object_or_404(ChildMilestone, pk=pk, person__household=household)
    if request.method == "POST":
        form = ChildMilestoneForm(request.POST, instance=milestone, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{milestone.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = ChildMilestoneForm(instance=milestone, household=household)
    return render(request, "planner/form.html", {"title": f"Edit {milestone.name}", "form": form})


@feature_required("salary_changes")
def salary_change_create(request):
    household = get_household()
    if request.method == "POST":
        form = SalaryChangeForm(request.POST, household=household)
        if form.is_valid():
            change = form.save()
            messages.success(request, f"{change.name} added.")
            return redirect("planner:plan_index")
    else:
        form = SalaryChangeForm(household=household)
    return render(
        request,
        "planner/form.html",
        {"title": "Add salary change", "form": form, "form_hint": SALARY_CHANGE_HINT},
    )


@feature_required("salary_changes")
def salary_change_update(request, pk):
    household = get_household()
    change = get_object_or_404(SalaryChange, pk=pk, person__household=household)
    if request.method == "POST":
        form = SalaryChangeForm(request.POST, instance=change, household=household)
        if form.is_valid():
            form.save()
            messages.success(request, f"{change.name} updated.")
            return redirect("planner:plan_index")
    else:
        form = SalaryChangeForm(instance=change, household=household)
    return render(
        request,
        "planner/form.html",
        {"title": f"Edit {change.name}", "form": form, "form_hint": SALARY_CHANGE_HINT},
    )
