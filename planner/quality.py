from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AssetAccount,
    CashGoal,
    Debt,
    DepotHolding,
    EquityGrant,
    IncomeInvestment,
    MoneyRule,
    Person,
    PlannedInvestmentPurchase,
    PrivateLoanReceivable,
    RetirementPlan,
    SalaryChange,
    TrueExpense,
)
from .projection_integrity import check_projection_integrity
from .projections import build_projection, build_yearly_projection, first_of_month, summarize_debt
from .retirement import retirement_tax_summary


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    category: str
    title: str
    detail: str
    action_url: str = ""
    action_label: str = ""


def infer_category(title, action_url):
    value = f"{title} {action_url}".lower()
    if "backup" in value or "login" in value or "debug" in value or "system" in value:
        return "Security & Operations"
    if "person" in value or "adult" in value or "child" in value:
        return "Household"
    if "account" in value or "depot" in value or "holding" in value or "savings" in value or "loan" in value:
        return "Accounts & Depot"
    if "debt" in value or "principal" in value or "fixed interest" in value:
        return "Debt"
    if "retirement" in value or "pension" in value:
        return "Retirement"
    if "cash goal" in value or "projection" in value or "rule" in value or "expense" in value:
        return "Projection"
    if "investment" in value or "equity" in value or "withholding" in value:
        return "Income & Investments"
    return "General"


def issue(severity, title, detail, action_url="", action_label="", category=""):
    category = category or infer_category(title, action_url)
    return QualityIssue(severity, category, title, detail, action_url, action_label)


def checklist_item(label, complete, detail, action_url="", action_label=""):
    return {
        "label": label,
        "complete": complete,
        "status": "complete" if complete else "attention",
        "detail": detail,
        "action_url": action_url,
        "action_label": action_label,
    }


def build_completeness_checklist(household, accounts, rules, people, latest_backup, today):
    stale_cutoff = today - timedelta(days=45)
    backup_cutoff = today - timedelta(days=7)
    adults = [person for person in people if person.role == Person.Role.ADULT]
    active_income_rules = [rule for rule in rules if rule.is_active and rule.kind == MoneyRule.Kind.INCOME]
    active_retirement_person_ids = set(
        RetirementPlan.objects.filter(household=household, is_active=True).values_list("person_id", flat=True)
    )
    stale_accounts = [account for account in accounts if account.as_of_date and account.as_of_date < stale_cutoff]
    tax_defaults = (
        household.pension_tax_rate == Decimal("18.00")
        and household.capital_gains_tax_rate == Decimal("25.00")
        and household.church_tax_rate == Decimal("0.00")
        and household.solidarity_surcharge_rate == Decimal("0.00")
        and household.health_insurance_rate == Decimal("11.00")
    )
    latest_backup_fresh = False
    if latest_backup is not None:
        backup_date = datetime.fromtimestamp(latest_backup.stat().st_mtime).date()
        latest_backup_fresh = backup_date >= backup_cutoff

    items = [
        checklist_item(
            "Household people",
            bool(people) and bool(adults),
            "At least one adult is configured." if adults else "Add adults and children so income, costs, and pensions can be attributed.",
            reverse("planner:person_create"),
            "Add person",
        ),
        checklist_item(
            "Account foundation",
            bool(accounts) and any(account.is_liquid for account in accounts),
            "At least one liquid account exists." if any(account.is_liquid for account in accounts) else "Add cash or savings accounts before trusting liquidity.",
            reverse("planner:account_create"),
            "Add account",
        ),
        checklist_item(
            "Recurring income",
            bool(active_income_rules),
            "Active income rules drive the base cash-flow forecast." if active_income_rules else "Add salary, benefits, pension, or other recurring income.",
            reverse("planner:rule_create"),
            "Add income",
        ),
        checklist_item(
            "Cash goals",
            household.cash_goals.filter(is_active=True).exists(),
            "Active yearly cash goals make FIRE and retirement draw needs visible.",
            reverse("planner:cash_goal_create"),
            "Add cash goal",
        ),
        checklist_item(
            "Assumptions reviewed",
            not tax_defaults,
            "Tax and retirement deduction assumptions were changed from defaults." if not tax_defaults else "Review tax, inflation, capital-income, and retirement assumptions before relying on real-data forecasts.",
            reverse("planner:assumptions_registry"),
            "Review assumptions",
        ),
        checklist_item(
            "Fresh valuations",
            not stale_accounts,
            "No account valuation date is older than 45 days." if not stale_accounts else f"{len(stale_accounts)} account valuation date(s) are older than 45 days.",
            reverse("planner:import_center"),
            "Refresh values",
        ),
        checklist_item(
            "Retirement coverage",
            bool(adults) and all(person.pk in active_retirement_person_ids for person in adults),
            "Each adult has an active retirement plan." if adults else "Add adults first, then pension assumptions.",
            reverse("planner:retirement_plan_create"),
            "Add pension",
        ),
        checklist_item(
            "Recent local backup",
            latest_backup_fresh,
            "A local backup exists from the last 7 days." if latest_backup_fresh else "Create a fresh local backup before importing or editing real data.",
            reverse("planner:backup_center"),
            "Open backups",
        ),
    ]
    complete_count = sum(1 for item in items if item["complete"])
    return {
        "items": items,
        "complete_count": complete_count,
        "total_count": len(items),
        "percent": round((complete_count / len(items)) * 100) if items else 100,
    }


def recurring_monthly_expenses(rules):
    return sum(
        (
            rule.monthly_amount
            for rule in rules
            if rule.is_active and rule.kind == MoneyRule.Kind.EXPENSE
        ),
        Decimal("0.00"),
    )


def emergency_fund_target(household, rules):
    if household.emergency_fund_months <= 0:
        return Decimal("0.00")
    return recurring_monthly_expenses(rules) * household.emergency_fund_months


def looks_like_manual_distribution_income(rule):
    value = f"{rule.name} {rule.category} {rule.notes}".lower()
    return any(token in value for token in ("distribution", "dividend", "ausschütt", "dividende"))


def looks_like_bond_or_maturity_holding(holding):
    value = f"{holding.name} {holding.asset_class} {holding.notes}".lower()
    return any(token in value for token in ("bond", "ibond", "maturity", "target maturity", "anleihe", "fällig"))


def latest_backup_file():
    backup_dir = Path(settings.BACKUP_DIR)
    if not backup_dir.exists():
        return None
    backups = [path for path in backup_dir.glob("*.sqlite3") if path.is_file()]
    if not backups:
        return None
    return max(backups, key=lambda path: path.stat().st_mtime)


def build_retirement_health_issues(household, projection=None, yearly_projection=None):
    projection = projection if projection is not None else build_projection(household)
    yearly_projection = (
        yearly_projection
        if yearly_projection is not None
        else build_yearly_projection(projection, household.cash_goals.filter(is_active=True))
    )
    issues = []
    active_plans = list(RetirementPlan.objects.filter(household=household, is_active=True))
    if not projection:
        return issues

    horizon_end = projection[-1].month
    if active_plans and all(plan.retirement_start_month > horizon_end for plan in active_plans):
        first_start = min(plan.retirement_start_month for plan in active_plans)
        issues.append(
            issue(
                "warning",
                "Planning horizon ends before retirement starts",
                f"The projection ends in {horizon_end:%b %Y}, before the first active retirement plan starts in {first_start:%b %Y}.",
                reverse("planner:household_settings"),
                "Extend horizon",
                category="Retirement",
            )
        )

    retirement_years = [item for item in yearly_projection if item.retirement_income > 0]
    if not retirement_years:
        return issues

    missing_goal_years = [item.label for item in retirement_years if item.annual_cash_goal <= Decimal("0.00")]
    if missing_goal_years:
        shown = ", ".join(missing_goal_years[:3])
        suffix = "..." if len(missing_goal_years) > 3 else ""
        issues.append(
            issue(
                "warning",
                "Retirement years have no cash goal",
                f"Add yearly cash goals for retirement years such as {shown}{suffix} so draw needs are visible.",
                reverse("planner:cash_goal_create"),
                "Add cash goal",
                category="Retirement",
            )
        )

    default_tax_assumptions = (
        household.pension_tax_rate == Decimal("18.00")
        and household.capital_gains_tax_rate == Decimal("25.00")
        and household.church_tax_rate == Decimal("0.00")
        and household.solidarity_surcharge_rate == Decimal("0.00")
        and household.health_insurance_rate == Decimal("11.00")
    )
    if default_tax_assumptions:
        issues.append(
            issue(
                "info",
                "Retirement tax assumptions still use defaults",
                "Review the household tax and health-insurance percentages before relying on tax-aware retirement draw numbers.",
                reverse("planner:household_settings"),
                "Review assumptions",
                category="Retirement",
            )
        )

    tax_summaries = [(item, retirement_tax_summary(item, household)) for item in retirement_years]
    high_draw_years = [item for item, summary in tax_summaries if summary["tax_aware_draw_percent"] > Decimal("4.00")]
    if len(high_draw_years) >= 2:
        issues.append(
            issue(
                "warning",
                "Tax-aware draw exceeds 4% in multiple retirement years",
                f"{len(high_draw_years)} retirement years need more than 4% of opening depot value after estimated tax drag.",
                reverse("planner:analytics"),
                "Open analytics",
                category="Retirement",
            )
        )

    no_depot_draw_year = next(
        (
            item
            for item, summary in tax_summaries
            if summary["gross_draw_for_net_cash"] > Decimal("0.00")
            and item.opening_invested_balance <= Decimal("0.00")
        ),
        None,
    )
    if no_depot_draw_year:
        issues.append(
            issue(
                "warning",
                "Retirement draw needed but depot is zero",
                f"{no_depot_draw_year.label} requires ETF/cash draw, but the opening depot value is zero.",
                reverse("planner:account_create"),
                "Add depot",
                category="Retirement",
            )
        )

    return issues


def build_quality_report(household):
    accounts = list(household.accounts.prefetch_related("holdings"))
    planned_purchases = list(household.planned_investment_purchases.select_related("target_account"))
    rules = list(household.rules.all())
    people = list(household.people.all())
    issues = []
    today = timezone.localdate()
    stale_cutoff = today - timedelta(days=45)
    backup_cutoff = today - timedelta(days=7)

    if not people:
        issues.append(issue("warning", "No people configured", "Add household members so income and expenses can be attributed.", reverse("planner:person_create"), "Add person"))
    elif not any(person.role == Person.Role.ADULT for person in people):
        issues.append(issue("warning", "No adult in household", "At least one adult usually makes the household model easier to reason about.", reverse("planner:person_create"), "Add person"))

    latest_backup = latest_backup_file()
    completeness = build_completeness_checklist(household, accounts, rules, people, latest_backup, today)
    if latest_backup is None:
        issues.append(issue("warning", "No local backup found", "Create a backup before importing or editing real financial data.", reverse("planner:backup_center"), "Open backups"))
    else:
        backup_date = datetime.fromtimestamp(latest_backup.stat().st_mtime).date()
        if backup_date < backup_cutoff:
            issues.append(issue("warning", "Latest local backup is stale", f"Latest backup is from {backup_date:%Y-%m-%d}.", reverse("planner:backup_center"), "Open backups"))

    if not getattr(settings, "LIF_REQUIRE_LOGIN", False):
        issues.append(issue("info", "Login protection is disabled", "Enable LIF_REQUIRE_LOGIN before exposing real data on a shared or always-on machine.", reverse("planner:system_status"), "System status"))
    if getattr(settings, "DEBUG", False):
        issues.append(issue("info", "Debug mode is enabled", "Disable DEBUG for production-style deployments that contain real financial data.", reverse("planner:system_status"), "System status"))

    if not accounts:
        issues.append(issue("critical", "No accounts configured", "Add at least one cash, savings, depot, or loan account before relying on projections.", reverse("planner:account_create"), "Add account"))
    elif not any(account.is_liquid for account in accounts):
        issues.append(issue("critical", "No liquid account", "Liquidity stress checks need at least one cash or savings account.", reverse("planner:account_create"), "Add account"))

    if not any(rule.is_active and rule.kind == MoneyRule.Kind.INCOME for rule in rules):
        issues.append(issue("warning", "No active income rule", "Projected cash flow has no recurring income rule.", reverse("planner:rule_create"), "Add rule"))
    else:
        active_income_person_ids = {
            rule.person_id
            for rule in rules
            if rule.is_active and rule.kind == MoneyRule.Kind.INCOME and rule.person_id
        }
        for person in people:
            if person.role == Person.Role.ADULT and person.pk not in active_income_person_ids:
                issues.append(issue("info", f"{person.name} has no active income rule", "Add salary, pension, or other income if this adult contributes cash flow.", reverse("planner:rule_create"), "Add rule"))
        if any(person.role == Person.Role.CHILD for person in people) and not any(
            person.role == Person.Role.CHILD and person.pk in active_income_person_ids for person in people
        ):
            issues.append(issue("info", "No child benefit income assigned", "Kindergeld or similar child-related income can be modeled as income assigned to a child.", reverse("planner:rule_create"), "Add rule"))

    target = emergency_fund_target(household, rules)
    if target > 0:
        projection = build_projection(household)
        lowest_month = min(projection, key=lambda item: item.liquid_balance) if projection else None
        if lowest_month and lowest_month.liquid_balance < target:
            issues.append(
                issue(
                    "warning",
                    "Emergency fund target is not met",
                    f"Lowest projected liquid balance is {lowest_month.liquid_balance:.2f}, below the configured target of {target:.2f}.",
                    reverse("planner:household_settings"),
                    "Review target",
                    category="Projection",
                )
            )

    for rule in rules:
        if rule.start_month and rule.end_month and rule.end_month < rule.start_month:
            issues.append(issue("critical", f"{rule.name} ends before it starts", "Fix the rule date range.", reverse("planner:rule_update", args=[rule.pk]), "Edit rule"))

    unrouted_rules = [rule for rule in rules if rule.is_active and not rule.account_id]
    if unrouted_rules and not household.default_operating_account_id:
        issues.append(issue(
            "info",
            "Recurring rules are not routed to an account",
            (
                f"{len(unrouted_rules)} active recurring rule(s) affect only the general liquid pool. "
                "Set a default operating account or choose cash-flow accounts on the rules for account-level forecasts."
            ),
            reverse("planner:household_settings"),
            "Set default account",
            category="Projection",
        ))

    if not household.default_operating_account_id:
        unrouted_future_count = (
            TrueExpense.objects.filter(household=household, is_active=True, account__isnull=True).count()
            + EquityGrant.objects.filter(household=household, is_active=True, account__isnull=True).count()
            + SalaryChange.objects.filter(person__household=household, is_active=True, account__isnull=True).count()
        )
        if unrouted_future_count:
            issues.append(issue(
                "info",
                "Future cash-flow items are not routed to an account",
                (
                    f"{unrouted_future_count} active future cash-flow item(s) affect only the general liquid pool. "
                    "Set a default operating account or choose accounts on salary changes, true expenses, and equity grants."
                ),
                reverse("planner:household_settings"),
                "Set default account",
                category="Projection",
            ))

    if not household.cash_goals.filter(is_active=True).exists():
        issues.append(issue("info", "No active cash goal", "Add a yearly cash need to compare retirement income with ETF draw requirements.", reverse("planner:cash_goal_create"), "Add cash goal"))

    if household.fund_cash_goal_from_depot and recurring_monthly_expenses(rules) > Decimal("0.00"):
        issues.append(issue(
            "warning",
            "Cash goal draw may double-count expenses",
            (
                "Portfolio funding treats the yearly cash goal as spending. If your active expense rules already "
                "represent the same household spending, liquidity can be reduced twice."
            ),
            reverse("planner:cash_goal_index"),
            "Review cash goals",
            category="Projection",
        ))

    for goal in household.cash_goals.all():
        if goal.end_year and goal.end_year < goal.start_year:
            issues.append(issue("critical", f"{goal.name} has an invalid year range", "The end year is before the start year.", reverse("planner:cash_goal_update", args=[goal.pk]), "Edit cash goal"))

    has_direct_depot_distributions = any(
        account.account_type == AssetAccount.AccountType.DEPOT
        and (
            (not account.uses_holdings_valuation and account.depot_annual_distribution_rate > Decimal("0.00"))
            or any(holding.annual_distribution_rate > Decimal("0.00") for holding in account.holdings.all())
        )
        for account in accounts
    ) or any(
        purchase.is_active
        and purchase.target_account.uses_holdings_valuation
        and purchase.annual_distribution_rate > Decimal("0.00")
        for purchase in planned_purchases
    )
    has_manual_distribution_income = any(
        rule.is_active
        and rule.kind == MoneyRule.Kind.INCOME
        and looks_like_manual_distribution_income(rule)
        for rule in rules
    )
    if has_direct_depot_distributions and has_manual_distribution_income:
        issues.append(issue(
            "warning",
            "Depot distributions may be double-counted",
            (
                "At least one depot has a direct distribution yield and an active income rule looks like manual "
                "dividend/distribution income. Keep only one model for the same payouts."
            ),
            reverse("planner:plan_index"),
            "Review rules",
            category="Income & Investments",
        ))

    for account in accounts:
        if account.as_of_date and account.as_of_date < stale_cutoff:
            issues.append(issue("warning", f"{account.name} has stale account data", f"Last account date is {account.as_of_date:%Y-%m-%d}.", reverse("planner:account_update", args=[account.pk]), "Edit account"))

        if account.currency and account.currency != household.currency:
            issues.append(issue("warning", f"{account.name} uses a non-household currency", f"{account.name} is in {account.currency} but the household plans in {household.currency}. The projection sums balances without currency conversion, so mixed currencies distort every total.", reverse("planner:account_update", args=[account.pk]), "Edit account"))

        if (
            account.account_type == AssetAccount.AccountType.SAVINGS
            and account.balance > Decimal("0.00")
            and account.savings_annual_interest_rate == Decimal("0.00")
        ):
            issues.append(issue("info", f"{account.name} has no savings interest rate", "Add the current savings interest rate so monthly or quarterly interest is projected.", reverse("planner:account_update", args=[account.pk]), "Edit account"))

        if account.account_type == AssetAccount.AccountType.DEPOT:
            holdings = list(account.holdings.all())
            if account.depot_valuation == AssetAccount.DepotValuation.HOLDINGS_SUM and not holdings:
                issues.append(issue("critical", f"{account.name} uses holdings valuation without holdings", "Add holdings or switch the depot valuation back to account balance.", reverse("planner:account_update", args=[account.pk]), "Edit account"))
            difference = account.depot_difference
            if holdings and abs(difference) > Decimal("1.00"):
                issues.append(issue("warning", f"{account.name} balance differs from holdings", f"Holdings and account balance differ by {difference.quantize(Decimal('0.01'))} {account.currency}.", reverse("planner:account_update", args=[account.pk]), "Review depot"))

        if account.account_type == AssetAccount.AccountType.LOAN and not hasattr(account, "debt"):
            issues.append(issue("warning", f"{account.name} is a loan without debt details", "Add a debt model so interest, principal, and refinance assumptions are projected.", reverse("planner:debt_create"), "Add debt"))

    for holding in DepotHolding.objects.filter(asset_account__household=household):
        if holding.quantity == Decimal("0.000000"):
            issues.append(issue("info", f"{holding.name} has zero quantity", "Remove sold positions or update the quantity if this holding should still affect depot valuation.", reverse("planner:depot_holding_update", args=[holding.pk]), "Edit holding"))
        if holding.latest_price <= Decimal("0.00"):
            issues.append(issue("critical", f"{holding.name} has no usable latest price", "Set a positive latest price before relying on holdings-based depot valuation.", reverse("planner:depot_holding_update", args=[holding.pk]), "Edit holding"))
        if not holding.isin and not holding.ticker:
            issues.append(issue("info", f"{holding.name} has no ISIN or ticker", "Identifiers make future price/import matching easier.", reverse("planner:depot_holding_update", args=[holding.pk]), "Edit holding"))
        if holding.currency and holding.currency != household.currency:
            issues.append(issue("warning", f"{holding.name} uses a non-household currency", f"{holding.name} is priced in {holding.currency} but the household plans in {household.currency}. Holdings feed depot value without conversion, so enter prices in {household.currency}.", reverse("planner:depot_holding_update", args=[holding.pk]), "Edit holding"))
        if holding.as_of_date and holding.as_of_date < stale_cutoff:
            issues.append(issue("warning", f"{holding.name} has stale price data", f"Latest price date is {holding.as_of_date:%Y-%m-%d}.", reverse("planner:depot_holding_update", args=[holding.pk]), "Edit holding"))
        if holding.payout_date and holding.payout_amount is None and looks_like_bond_or_maturity_holding(holding):
            issues.append(issue("info", f"{holding.name} has no explicit payout amount", "Add an expected payout amount so bond or target-maturity gains are visible and taxed correctly at maturity.", reverse("planner:depot_holding_update", args=[holding.pk]), "Edit holding"))

    upcoming_refi_cutoff = today + timedelta(days=365)
    debt_horizon_start = first_of_month(household.start_month)
    for debt in Debt.objects.filter(household=household, is_active=True).select_related("account"):
        if abs(abs(debt.account.balance) - debt.current_principal) > Decimal("1.00"):
            issues.append(issue("warning", f"{debt.name} principal differs from loan account", "Debt principal and linked loan account balance should normally match.", reverse("planner:debt_update", args=[debt.pk]), "Edit debt"))
        if debt.fixed_interest_until and debt.fixed_interest_until <= upcoming_refi_cutoff:
            if debt.refinance_annual_interest_rate is None or debt.refinance_monthly_payment is None:
                issues.append(issue("warning", f"{debt.name} fixed interest ends soon", "Add refinance interest and payment assumptions before the fixed period ends.", reverse("planner:debt_update", args=[debt.pk]), "Edit debt"))
        debt_summary = summarize_debt(debt, debt_horizon_start)
        if debt.end_month and debt_summary["ending_principal"] > Decimal("1.00"):
            issues.append(issue("warning", f"{debt.name} does not fully amortize by its end month", f"About {debt_summary['ending_principal']} {debt.account.currency} is still owed when the debt's end month is reached; that residual is then ignored by the projection. Extend the end month, raise the payment, or clear the end month.", reverse("planner:debt_update", args=[debt.pk]), "Edit debt"))
        elif debt_summary["payoff_month"] is None and debt.current_principal > Decimal("0.00"):
            issues.append(issue("warning", f"{debt.name} payment may not cover interest", "The monthly payment never reduces the balance to zero, which usually means it does not cover the interest. Increase the monthly payment.", reverse("planner:debt_update", args=[debt.pk]), "Edit debt"))

    active_cash_goals = list(CashGoal.objects.filter(household=household, is_active=True))
    overlap_pair = None
    for index, goal_a in enumerate(active_cash_goals):
        for goal_b in active_cash_goals[index + 1:]:
            a_end = goal_a.end_year or 9999
            b_end = goal_b.end_year or 9999
            if goal_a.start_year <= b_end and goal_b.start_year <= a_end:
                overlap_pair = (goal_a, goal_b)
                break
        if overlap_pair:
            break
    if overlap_pair:
        issues.append(issue(
            "warning",
            "Cash goals overlap",
            f"'{overlap_pair[0].name}' and '{overlap_pair[1].name}' both apply to overlapping years; "
            "the goal with the later start year is used. Set end years so each year maps to one goal.",
            reverse("planner:cash_goal_index"),
            "Review cash goals",
        ))

    for investment in IncomeInvestment.objects.filter(household=household):
        if investment.end_month < investment.start_month:
            issues.append(issue("critical", f"{investment.name} ends before it starts", "Fix the investment date range.", reverse("planner:income_investment_update", args=[investment.pk]), "Edit investment"))
        if investment.principal > Decimal("0.00") and investment.monthly_income <= Decimal("0.00"):
            issues.append(issue("warning", f"{investment.name} has principal but no income", "Add expected monthly income or mark the investment inactive if it no longer pays out.", reverse("planner:income_investment_update", args=[investment.pk]), "Edit investment"))

    for loan in PrivateLoanReceivable.objects.filter(household=household, is_active=True):
        if loan.disbursement_month and loan.source_account_id is None:
            issues.append(issue(
                "warning",
                f"{loan.name} has future disbursement without source account",
                "Choose the cash or savings account that will fund the future loan so account-level liquidity is accurate.",
                reverse("planner:private_loan_update", args=[loan.pk]),
                "Edit loan",
                category="Income & Investments",
            ))

    for purchase in PlannedInvestmentPurchase.objects.filter(household=household, is_active=True):
        if (
            purchase.asset_type == PlannedInvestmentPurchase.AssetType.BOND
            and purchase.payout_date
            and purchase.payout_amount is None
        ):
            issues.append(issue(
                "info",
                f"{purchase.name} has no explicit payout amount",
                "Add an expected maturity payout so planned bond gains are visible and taxed correctly.",
                reverse("planner:planned_investment_purchase_update", args=[purchase.pk]),
                "Edit purchase",
                category="Income & Investments",
            ))

    active_retirement_person_ids = set(
        RetirementPlan.objects.filter(household=household, is_active=True).values_list("person_id", flat=True)
    )
    for person in people:
        if person.role == Person.Role.ADULT and person.pk not in active_retirement_person_ids:
            issues.append(issue("warning", f"{person.name} has no retirement plan", "Add statutory pension, Direktversicherung, or private pension assumptions for retirement projections.", reverse("planner:retirement_plan_create"), "Add pension"))

    for plan in RetirementPlan.objects.filter(household=household):
        if plan.retirement_start_month < household.start_month:
            issues.append(issue("critical", f"{plan.name} starts before the projection", "Set retirement start month inside or after the planning timeline.", reverse("planner:retirement_plan_update", args=[plan.pk]), "Edit pension"))
        if plan.end_month and plan.end_month < plan.retirement_start_month:
            issues.append(issue("critical", f"{plan.name} ends before it starts", "Fix the pension date range.", reverse("planner:retirement_plan_update", args=[plan.pk]), "Edit pension"))
        if (
            plan.current_pension_points == Decimal("0.000")
            and plan.expected_annual_points == Decimal("0.000")
            and plan.private_monthly_pension == Decimal("0.00")
        ):
            issues.append(issue("warning", f"{plan.name} has no pension value", "Enter pension points, expected accrual, or a private monthly pension amount.", reverse("planner:retirement_plan_update", args=[plan.pk]), "Edit pension"))

    for grant in EquityGrant.objects.filter(household=household):
        if grant.last_vest_month < grant.first_vest_month:
            issues.append(issue("critical", f"{grant.name} ends before it starts", "Fix the equity grant vesting range.", reverse("planner:equity_grant_update", args=[grant.pk]), "Edit equity"))
        if grant.withholding_rate < Decimal("0.00") or grant.withholding_rate > Decimal("100.00"):
            issues.append(issue("critical", f"{grant.name} has invalid withholding", "Withholding must be between 0% and 100%.", reverse("planner:equity_grant_update", args=[grant.pk]), "Edit equity"))

    for expense in TrueExpense.objects.filter(household=household):
        if expense.end_month and expense.end_month < expense.first_due_month:
            issues.append(issue("critical", f"{expense.name} ends before it starts", "Fix the true expense date range.", reverse("planner:true_expense_update", args=[expense.pk]), "Edit expense"))

    projection = build_projection(household)
    if projection:
        lowest = min(projection, key=lambda item: item.liquid_balance)
        if lowest.liquid_balance < 0:
            label = lowest.month.strftime("%b %Y")
            issues.append(issue("critical", "Projection has negative liquidity", f"Lowest projected liquid balance is {lowest.liquid_balance.quantize(Decimal('0.01'))} {household.currency} in {label}.", reverse("planner:analytics"), "Open analytics"))
        account_by_id = {account.id: account for account in accounts}
        lowest_by_account = {}
        for month in projection:
            for account_id, balance in month.account_balances.items():
                if account_id not in lowest_by_account or balance < lowest_by_account[account_id]["balance"]:
                    lowest_by_account[account_id] = {"month": month, "balance": balance}
        for account_id, row in lowest_by_account.items():
            account = account_by_id.get(account_id)
            if account and account.is_liquid and row["balance"] < Decimal("0.00"):
                label = row["month"].month.strftime("%b %Y")
                issues.append(issue(
                    "critical",
                    f"{account.name} goes negative in the account forecast",
                    f"Lowest projected account balance is {row['balance'].quantize(Decimal('0.01'))} {account.currency} in {label}. Review planned transfers and source accounts.",
                    reverse("planner:transfer_plan"),
                    "Open transfers",
                    category="Projection",
                ))
        yearly_projection = build_yearly_projection(projection, household.cash_goals.filter(is_active=True))
        integrity = check_projection_integrity(projection, yearly_projection, accounts)
        for failure in integrity["failures"][:5]:
            issues.append(issue(
                "critical",
                f"Projection integrity failed: {failure['check']}",
                (
                    f"{failure['scope']} {failure['label']} expected {failure['expected']} but calculated "
                    f"{failure['actual']} (difference {failure['difference']}). This means displayed totals "
                    "do not reconcile and the projection code needs review."
                ),
                reverse("planner:data_quality"),
                "Open quality",
                category="Projection",
            ))
        issues.extend(build_retirement_health_issues(household, projection, yearly_projection))
    else:
        integrity = {"ok": True, "checked": 0, "failures": [], "failure_count": 0}

    counts = {"critical": 0, "warning": 0, "info": 0}
    category_counts = {}
    for item in issues:
        counts[item.severity] += 1
        category_counts[item.category] = category_counts.get(item.category, 0) + 1
    grouped_issues = [
        {
            "category": category,
            "count": category_counts[category],
            "issues": [item for item in issues if item.category == category],
        }
        for category in sorted(category_counts)
    ]
    return {
        "issues": issues,
        "grouped_issues": grouped_issues,
        "category_counts": category_counts,
        "counts": counts,
        "total": len(issues),
        "integrity": integrity,
        "completeness": completeness,
    }
