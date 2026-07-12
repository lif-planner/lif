from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.urls import reverse

from .finance import money_value, real_value
from .models import (
    CashGoal,
    ChildMilestone,
    Debt,
    DepotHolding,
    EquityGrant,
    IncomeInvestment,
    MoneyRule,
    PlannedInvestmentPurchase,
    RetirementPlan,
    SalaryChange,
)
from .projections import build_projection, build_yearly_projection
from .retirement import retirement_tax_summary


def year_flow_index(item):
    # Representative discount point for a year's flows: its midpoint.
    return item.start_index + item.month_count // 2


def analytics_point(
    label,
    months_from_start,
    annual_inflation_rate,
    liquid_balance,
    net_worth,
    invested_balance,
    liability_balance,
    income,
    depot_payout,
    savings_interest_income,
    expenses,
    transfers,
    net,
    retirement_income,
    annual_cash_goal=Decimal("0.00"),
    cash_goal_coverage_percent=Decimal("0.00"),
    cash_goal_gap=Decimal("0.00"),
    portfolio_draw_percent=Decimal("0.00"),
    income_rule_income=Decimal("0.00"),
    depot_income=Decimal("0.00"),
    depot_draw=Decimal("0.00"),
    flow_months_from_start=None,
    detail_url="",
):
    # Balances are point-in-time (discounted at the period end); flows accrue
    # across the period, so they discount at the period midpoint. They coincide
    # for monthly points (flow_months_from_start defaults to months_from_start).
    flow_index = months_from_start if flow_months_from_start is None else flow_months_from_start
    return {
        "label": label,
        "detailUrl": detail_url,
        "monthsFromStart": months_from_start,
        "liquidBalance": money_value(liquid_balance),
        "liquidBalanceReal": money_value(real_value(liquid_balance, annual_inflation_rate, months_from_start)),
        "netWorth": money_value(net_worth),
        "netWorthReal": money_value(real_value(net_worth, annual_inflation_rate, months_from_start)),
        "depotValue": money_value(invested_balance),
        "depotValueReal": money_value(real_value(invested_balance, annual_inflation_rate, months_from_start)),
        "liabilityBalance": money_value(liability_balance),
        "liabilityBalanceReal": money_value(real_value(liability_balance, annual_inflation_rate, months_from_start)),
        "income": money_value(income),
        "incomeReal": money_value(real_value(income, annual_inflation_rate, flow_index)),
        "incomeRuleIncome": money_value(income_rule_income),
        "incomeRuleIncomeReal": money_value(real_value(income_rule_income, annual_inflation_rate, flow_index)),
        "depotIncome": money_value(depot_income),
        "depotIncomeReal": money_value(real_value(depot_income, annual_inflation_rate, flow_index)),
        "depotDraw": money_value(depot_draw),
        "depotDrawReal": money_value(real_value(depot_draw, annual_inflation_rate, flow_index)),
        "depotPayout": money_value(depot_payout),
        "depotPayoutReal": money_value(real_value(depot_payout, annual_inflation_rate, flow_index)),
        "savingsInterestIncome": money_value(savings_interest_income),
        "savingsInterestIncomeReal": money_value(real_value(savings_interest_income, annual_inflation_rate, flow_index)),
        "expenses": money_value(expenses),
        "expensesReal": money_value(real_value(expenses, annual_inflation_rate, flow_index)),
        "transfers": money_value(transfers),
        "transfersReal": money_value(real_value(transfers, annual_inflation_rate, flow_index)),
        "cashNet": money_value(net),
        "cashNetReal": money_value(real_value(net, annual_inflation_rate, flow_index)),
        "retirementIncome": money_value(retirement_income),
        "retirementIncomeReal": money_value(real_value(retirement_income, annual_inflation_rate, flow_index)),
        "annualCashGoal": money_value(annual_cash_goal),
        "annualCashGoalReal": money_value(real_value(annual_cash_goal, annual_inflation_rate, flow_index)),
        "cashGoalCoveragePercent": money_value(cash_goal_coverage_percent),
        "cashGoalGap": money_value(cash_goal_gap),
        "cashGoalGapReal": money_value(real_value(cash_goal_gap, annual_inflation_rate, flow_index)),
        "portfolioDrawPercent": money_value(portfolio_draw_percent),
    }


def tax_aware_analytics_fields(item, household):
    summary = retirement_tax_summary(item, household)
    rate = household.annual_inflation_rate
    flow_index = year_flow_index(item)
    return {
        "netIncome": money_value(summary["net_income"]),
        "netIncomeReal": money_value(real_value(summary["net_income"], rate, flow_index)),
        "netRetirementIncome": money_value(summary["net_retirement_income"]),
        "netRetirementIncomeReal": money_value(real_value(summary["net_retirement_income"], rate, flow_index)),
        "retirementDeductions": money_value(summary["retirement_deductions"]),
        "retirementDeductionsReal": money_value(real_value(summary["retirement_deductions"], rate, flow_index)),
        "taxAwareCashGap": money_value(summary["net_cash_gap"]),
        "taxAwareCashGapReal": money_value(real_value(summary["net_cash_gap"], rate, flow_index)),
        "capitalTaxDrag": money_value(summary["capital_tax_drag"]),
        "capitalTaxDragReal": money_value(real_value(summary["capital_tax_drag"], rate, flow_index)),
        "taxAwareDrawNeed": money_value(summary["gross_draw_for_net_cash"]),
        "taxAwareDrawNeedReal": money_value(real_value(summary["gross_draw_for_net_cash"], rate, flow_index)),
        "taxAwareDrawPercent": money_value(summary["tax_aware_draw_percent"]),
    }


def yearly_analytics_point(item, household, annual_inflation_rate, year_index):
    point = analytics_point(
        item.label,
        item.end_index,
        annual_inflation_rate,
        item.ending_liquid_balance,
        item.ending_net_worth,
        item.ending_invested_balance,
        item.ending_liability_balance,
        item.income,
        item.depot_payout,
        item.savings_interest_income,
        item.expenses,
        item.transfers,
        item.net,
        item.retirement_income,
        item.annual_cash_goal,
        item.cash_goal_coverage_percent,
        item.cash_goal_gap,
        item.portfolio_draw_percent,
        income_rule_income=item.income_rule_income,
        depot_income=item.depot_income,
        depot_draw=item.depot_draw,
        flow_months_from_start=year_flow_index(item),
        # The audit view indexes the yearly list by position; with calendar-year
        # buckets that no longer equals start_index // 12 (partial first year).
        detail_url=reverse("planner:projection_year_audit", args=[year_index]),
    )
    if household:
        point.update(tax_aware_analytics_fields(item, household))
    return point


def analytics_milestone(label, month, category, detail="", url=""):
    if not month:
        return None
    return {
        "label": label,
        "date": month.isoformat(),
        "month": month.strftime("%b %Y"),
        "year": str(month.year),
        "category": category,
        "detail": detail,
        "url": url,
    }


def build_analytics_milestones(household):
    if household is None:
        return []
    milestones = []
    for debt in Debt.objects.filter(household=household, is_active=True).select_related("account"):
        milestones.append(
            analytics_milestone(
                f"{debt.name} fixed interest ends",
                debt.fixed_interest_until,
                "Debt",
                "Refinance assumptions should be reviewed before this date.",
                reverse("planner:debt_update", args=[debt.pk]),
            )
        )
        milestones.append(
            analytics_milestone(
                f"{debt.name} planned payoff",
                debt.end_month,
                "Debt",
                "Debt schedule end date.",
                reverse("planner:debt_update", args=[debt.pk]),
            )
        )
    for plan in RetirementPlan.objects.filter(household=household, is_active=True).select_related("person"):
        milestones.append(
            analytics_milestone(
                f"{plan.person.name} retirement starts",
                plan.retirement_start_month,
                "Retirement",
                plan.name,
                reverse("planner:retirement_plan_update", args=[plan.pk]),
            )
        )
    for milestone in ChildMilestone.objects.filter(person__household=household, is_active=True).select_related("person"):
        milestones.append(
            analytics_milestone(
                f"{milestone.person.name}: {milestone.name}",
                milestone.start_month,
                "Children",
                "Child milestone starts.",
                reverse("planner:child_milestone_update", args=[milestone.pk]),
            )
        )
        milestones.append(
            analytics_milestone(
                f"{milestone.person.name}: {milestone.name} ends",
                milestone.end_month,
                "Children",
                "Child milestone ends.",
                reverse("planner:child_milestone_update", args=[milestone.pk]),
            )
        )
    for change in SalaryChange.objects.filter(person__household=household, is_active=True).select_related("person"):
        milestones.append(
            analytics_milestone(
                f"{change.person.name}: {change.name}",
                change.start_month,
                "Income",
                "Salary change starts.",
                reverse("planner:salary_change_update", args=[change.pk]),
            )
        )
    for investment in IncomeInvestment.objects.filter(household=household, is_active=True):
        milestones.append(
            analytics_milestone(
                f"{investment.name} starts",
                investment.start_month,
                "Income",
                "Income investment starts.",
                reverse("planner:income_investment_update", args=[investment.pk]),
            )
        )
        milestones.append(
            analytics_milestone(
                f"{investment.name} ends",
                investment.end_month,
                "Income",
                "Income investment ends.",
                reverse("planner:income_investment_update", args=[investment.pk]),
            )
        )
    for holding in DepotHolding.objects.filter(
        asset_account__household=household,
        payout_date__isnull=False,
    ).select_related("asset_account"):
        milestones.append(
            analytics_milestone(
                f"{holding.name} payout",
                holding.payout_date,
                "Depot",
                f"{money_value(holding.expected_payout_amount)} {holding.currency} from {holding.asset_account.name}",
                reverse("planner:depot_holding_update", args=[holding.pk]),
            )
        )
    for purchase in PlannedInvestmentPurchase.objects.filter(household=household, is_active=True).select_related("target_account"):
        milestones.append(
            analytics_milestone(
                f"{purchase.name} purchase",
                purchase.purchase_month,
                "Depot",
                f"{money_value(purchase.purchase_amount)} into {purchase.target_account.name}",
                reverse("planner:planned_investment_purchase_update", args=[purchase.pk]),
            )
        )
        if purchase.payout_date:
            milestones.append(
                analytics_milestone(
                    f"{purchase.name} payout",
                    purchase.payout_date,
                    "Depot",
                    f"{money_value(purchase.expected_payout_amount)} from {purchase.target_account.name}",
                    reverse("planner:planned_investment_purchase_update", args=[purchase.pk]),
                )
            )
    for grant in EquityGrant.objects.filter(household=household, is_active=True).select_related("person"):
        milestones.append(
            analytics_milestone(
                f"{grant.name} first vest",
                grant.first_vest_month,
                "Equity",
                grant.person.name,
                reverse("planner:equity_grant_update", args=[grant.pk]),
            )
        )
        milestones.append(
            analytics_milestone(
                f"{grant.name} last vest",
                grant.last_vest_month,
                "Equity",
                grant.person.name,
                reverse("planner:equity_grant_update", args=[grant.pk]),
            )
        )
    for goal in CashGoal.objects.filter(household=household, is_active=True):
        milestones.append(
            analytics_milestone(
                f"{goal.name} cash goal starts",
                date(goal.start_year, 1, 1),
                "Cash Goal",
                money_value(goal.annual_amount),
                reverse("planner:cash_goal_update", args=[goal.pk]),
            )
        )
        if goal.end_year:
            milestones.append(
                analytics_milestone(
                    f"{goal.name} cash goal ends",
                    date(goal.end_year, 12, 1),
                    "Cash Goal",
                    money_value(goal.annual_amount),
                    reverse("planner:cash_goal_update", args=[goal.pk]),
                )
            )
    for rule in MoneyRule.objects.filter(household=household, kind=MoneyRule.Kind.INCOME, is_active=True):
        if rule.start_month:
            milestones.append(
                analytics_milestone(
                    f"{rule.name} income starts",
                    rule.start_month,
                    "Income",
                    money_value(rule.monthly_amount),
                    reverse("planner:rule_update", args=[rule.pk]),
                )
            )
        if rule.end_month:
            milestones.append(
                analytics_milestone(
                    f"{rule.name} income ends",
                    rule.end_month,
                    "Income",
                    money_value(rule.monthly_amount),
                    reverse("planner:rule_update", args=[rule.pk]),
                )
            )
    return sorted((item for item in milestones if item), key=lambda item: (item["date"], item["category"], item["label"]))


def build_analytics_data(projection, yearly_projection, household=None):
    milestones = build_analytics_milestones(household)
    annual_inflation_rate = household.annual_inflation_rate if household else Decimal("0.00")
    return {
        "inflation": {
            "annualRate": money_value(annual_inflation_rate),
        },
        "monthly": [
            analytics_point(
                item.month.strftime("%b %Y"),
                item.index,
                annual_inflation_rate,
                item.liquid_balance,
                item.net_worth,
                item.invested_balance,
                item.liability_balance,
                item.income,
                item.depot_payout,
                item.savings_interest_income,
                item.expenses,
                item.transfers,
                item.net,
                item.retirement_income,
                income_rule_income=item.income_rule_income,
                depot_income=item.depot_income,
                depot_draw=item.depot_draw,
                detail_url=reverse("planner:projection_audit", args=[item.index]),
            )
            for item in projection
        ],
        "yearly": [
            yearly_analytics_point(item, household, annual_inflation_rate, index)
            for index, item in enumerate(yearly_projection)
        ],
        "milestones": {
            "monthly": milestones,
            "yearly": milestones,
        },
    }


def scenario_period_point(item, annual_inflation_rate, household=None):
    flow_index = year_flow_index(item)
    point = {
        "label": item.label,
        "liquidBalance": money_value(item.ending_liquid_balance),
        "liquidBalanceReal": money_value(real_value(item.ending_liquid_balance, annual_inflation_rate, item.end_index)),
        "netWorth": money_value(item.ending_net_worth),
        "netWorthReal": money_value(real_value(item.ending_net_worth, annual_inflation_rate, item.end_index)),
        "depotValue": money_value(item.ending_invested_balance),
        "depotValueReal": money_value(real_value(item.ending_invested_balance, annual_inflation_rate, item.end_index)),
        "liabilityBalance": money_value(item.ending_liability_balance),
        "liabilityBalanceReal": money_value(real_value(item.ending_liability_balance, annual_inflation_rate, item.end_index)),
        "cashGoalGap": money_value(item.cash_goal_gap),
        "cashGoalGapReal": money_value(real_value(item.cash_goal_gap, annual_inflation_rate, flow_index)),
        "retirementIncome": money_value(item.retirement_income),
        "retirementIncomeReal": money_value(real_value(item.retirement_income, annual_inflation_rate, flow_index)),
        "annualCashGoal": money_value(item.annual_cash_goal),
        "annualCashGoalReal": money_value(real_value(item.annual_cash_goal, annual_inflation_rate, flow_index)),
        "portfolioDrawPercent": money_value(item.portfolio_draw_percent),
    }
    if household:
        point.update(tax_aware_analytics_fields(item, household))
    return point


def scenario_input_diffs(scenario):
    if scenario is None:
        return [
            {
                "label": "Base household",
                "kind": "base",
                "amount": None,
                "detail": "Uses the active household inputs without scenario-level adjustments.",
            }
        ]
    rows = []
    if getattr(scenario, "is_preset", False):
        rows.append(
            {
                "label": "Preset stress test",
                "kind": "preset",
                "amount": None,
                "detail": getattr(scenario, "detail", "") or "Generated stress preset.",
            }
        )
    field_definitions = (
        ("liquid_balance_delta", "Starting cash adjustment", "one-time"),
        ("monthly_income_delta", "Monthly income adjustment", "monthly"),
        ("monthly_expense_delta", "Monthly expense adjustment", "monthly"),
    )
    for field_name, label, kind in field_definitions:
        amount = getattr(scenario, field_name, Decimal("0.00"))
        if amount:
            rows.append(
                {
                    "label": label,
                    "kind": kind,
                    "amount": amount,
                    "detail": "Scenario-level adjustment versus the active household.",
                }
            )
    if getattr(scenario, "notes", ""):
        rows.append(
            {
                "label": "Scenario notes",
                "kind": "notes",
                "amount": None,
                "detail": scenario.notes,
            }
        )
    return rows or [
        {
            "label": "No explicit input changes",
            "kind": "empty",
            "amount": None,
            "detail": "This scenario currently matches the active household inputs.",
        }
    ]


def scenario_outcome(label, scenario, projection, yearly_projection, currency, annual_inflation_rate, household):
    if not projection:
        return None
    lowest = min(projection, key=lambda item: item.liquid_balance)
    first_retirement = next((item for item in projection if item.retirement_income > 0), None)
    first_retirement_year = next((item for item in yearly_projection if item.retirement_income > 0), None)
    ending_year = yearly_projection[-1] if yearly_projection else None
    retirement_years = [item for item in yearly_projection if item.retirement_income > 0]
    tax_summaries = [retirement_tax_summary(item, household) for item in retirement_years]
    max_tax_aware_draw_percent = max(
        (summary["tax_aware_draw_percent"] for summary in tax_summaries),
        default=Decimal("0.00"),
    )
    years_above_four_percent = sum(1 for summary in tax_summaries if summary["tax_aware_draw_percent"] > Decimal("4.00"))
    ending_tax_summary = retirement_tax_summary(ending_year, household) if ending_year else None
    return {
        "label": label,
        "scenario": scenario,
        "is_preset": bool(getattr(scenario, "is_preset", False)),
        "detail": getattr(scenario, "detail", ""),
        "input_diffs": scenario_input_diffs(scenario),
        "currency": currency,
        "ending_liquid": projection[-1].liquid_balance,
        "ending_net_worth": projection[-1].net_worth,
        "ending_depot": projection[-1].invested_balance,
        "ending_liability": projection[-1].liability_balance,
        "lowest_liquid": lowest.liquid_balance,
        "lowest_label": lowest.month.strftime("%b %Y"),
        # Match the cash-stress definition used by the liquidity view and the
        # yearly projection: negative liquidity while still solvent.
        "stress_months": sum(1 for item in projection if item.liquid_balance < 0 <= item.net_worth),
        "retirement_label": first_retirement.month.strftime("%b %Y") if first_retirement else "",
        "retirement_income": first_retirement.retirement_income if first_retirement else Decimal("0.00"),
        "first_retirement_year": first_retirement_year.label if first_retirement_year else "",
        "ending_cash_goal_gap": ending_year.cash_goal_gap if ending_year else Decimal("0.00"),
        "ending_tax_aware_draw_need": ending_tax_summary["gross_draw_for_net_cash"] if ending_tax_summary else Decimal("0.00"),
        "max_tax_aware_draw_percent": max_tax_aware_draw_percent,
        "years_above_four_percent": years_above_four_percent,
        "series": [scenario_period_point(item, annual_inflation_rate, household) for item in yearly_projection],
    }


def projection_outcome_metrics(household, label, stress=None, annual_inflation_rate=None):
    stress = stress or {}
    cash_goals = household.cash_goals.all()
    annual_inflation_rate = annual_inflation_rate if annual_inflation_rate is not None else household.annual_inflation_rate
    projection = build_projection(household, stress=stress)
    yearly_projection = build_yearly_projection(
        projection,
        cash_goals,
        annual_inflation_rate=stress.get("annual_inflation_rate"),
        cash_goal_multiplier=stress.get("cash_goal_multiplier", Decimal("1.00")),
    )
    return scenario_outcome(
        label,
        None,
        projection,
        yearly_projection,
        household.currency,
        annual_inflation_rate,
        household,
    )


def build_assumption_sensitivity(household):
    groups = [
        {
            "key": "depot_return",
            "label": "Depot Return",
            "description": "Price-growth assumptions for depot accounts. Distributions and fixed payouts stay unchanged.",
            "rows": [
                projection_outcome_metrics(
                    household,
                    f"{rate}% depot return",
                    stress={"depot_annual_return_override": Decimal(str(rate))},
                )
                for rate in [0, 3, 5, 7]
                if household.accounts.filter(account_type="depot").exists()
            ],
        },
        {
            "key": "inflation",
            "label": "Inflation",
            "description": "Re-indexes inflation-linked cash goals and today's-money display assumptions.",
            "rows": [
                projection_outcome_metrics(
                    household,
                    f"{rate}% inflation",
                    stress={"annual_inflation_rate": Decimal(str(rate))},
                    annual_inflation_rate=Decimal(str(rate)),
                )
                for rate in [1, 2, 4]
            ],
        },
        {
            "key": "pension_adjustment",
            "label": "Pension Adjustment",
            "description": "Overrides annual German pension/Direktversicherung adjustment assumptions in the forecast.",
            "rows": [
                projection_outcome_metrics(
                    household,
                    f"{rate}% pension adjustment",
                    stress={"pension_adjustment_override": Decimal(str(rate))},
                )
                for rate in [0, 1, 2]
                if household.retirement_plans.exists()
            ],
        },
        {
            "key": "cash_goal",
            "label": "Cash Goal",
            "description": "Scales retirement/FIRE cash-goal spending needs while leaving income unchanged.",
            "rows": [
                projection_outcome_metrics(
                    household,
                    label,
                    stress={"cash_goal_multiplier": multiplier},
                )
                for label, multiplier in [
                    ("Cash goal -10%", Decimal("0.90")),
                    ("Cash goal base", Decimal("1.00")),
                    ("Cash goal +10%", Decimal("1.10")),
                ]
                if household.cash_goals.filter(is_active=True).exists()
            ],
        },
    ]
    return [group for group in groups if group["rows"]]


def stress_preset_definitions(household):
    income_rules = household.rules.filter(kind="income", is_active=True)
    expense_rules = household.rules.filter(kind="expense", is_active=True)
    active_family_gifts = household.family_gift_plans.filter(is_active=True).exists()
    active_property_transfers = household.real_estate_transfer_plans.filter(is_active=True).exists()
    active_owned_properties = any(
        property_item.is_active and property_item.acquired_before(household.start_month)
        for property_item in household.properties.all()
    )
    monthly_income = sum((rule.monthly_amount for rule in income_rules), Decimal("0.00"))
    monthly_expenses = sum((rule.monthly_amount for rule in expense_rules), Decimal("0.00"))
    presets = []
    if monthly_income:
        amount = -(monthly_income * Decimal("0.20")).quantize(Decimal("0.01"))
        presets.append({
            "label": "Stress: income -20%",
            "detail": f"Reduces active recurring income by {abs(amount)} per month.",
            "scenario": SimpleNamespace(
                name="Stress: income -20%",
                is_active=True,
                is_preset=True,
                detail=f"Reduces active recurring income by {abs(amount)} per month.",
                liquid_balance_delta=Decimal("0.00"),
                monthly_income_delta=amount,
                monthly_expense_delta=Decimal("0.00"),
            ),
            "stress": {},
        })
    if monthly_expenses:
        amount = (monthly_expenses * Decimal("0.10")).quantize(Decimal("0.01"))
        presets.append({
            "label": "Stress: expenses +10%",
            "detail": f"Increases active recurring expenses by {amount} per month.",
            "scenario": SimpleNamespace(
                name="Stress: expenses +10%",
                is_active=True,
                is_preset=True,
                detail=f"Increases active recurring expenses by {amount} per month.",
                liquid_balance_delta=Decimal("0.00"),
                monthly_income_delta=Decimal("0.00"),
                monthly_expense_delta=amount,
            ),
            "stress": {},
        })
    if household.accounts.filter(account_type="depot", depot_annual_return_rate__gt=0).exists():
        presets.append({
            "label": "Stress: depot return 0%",
            "detail": "Sets assumed depot price growth to 0% while keeping distributions and payouts.",
            "scenario": SimpleNamespace(
                name="Stress: depot return 0%",
                is_active=True,
                is_preset=True,
                detail="Sets assumed depot price growth to 0%.",
                liquid_balance_delta=Decimal("0.00"),
                monthly_income_delta=Decimal("0.00"),
                monthly_expense_delta=Decimal("0.00"),
            ),
            "stress": {"depot_annual_return_override": Decimal("0.00")},
        })
    if household.debts.filter(is_active=True).exists():
        presets.append({
            "label": "Stress: debt rates +2%",
            "detail": "Adds two percentage points to active debt interest rates for the projection.",
            "scenario": SimpleNamespace(
                name="Stress: debt rates +2%",
                is_active=True,
                is_preset=True,
                detail="Adds two percentage points to active debt rates.",
                liquid_balance_delta=Decimal("0.00"),
                monthly_income_delta=Decimal("0.00"),
                monthly_expense_delta=Decimal("0.00"),
            ),
            "stress": {"debt_rate_delta": Decimal("2.00")},
        })
    if active_property_transfers:
        presets.append({
            "label": "Estate: keep property",
            "detail": "Disables planned real-estate transfers so property value remains in household net worth.",
            "scenario": SimpleNamespace(
                name="Estate: keep property",
                is_active=True,
                is_preset=True,
                detail="Disables planned real-estate transfers for comparison.",
                liquid_balance_delta=Decimal("0.00"),
                monthly_income_delta=Decimal("0.00"),
                monthly_expense_delta=Decimal("0.00"),
            ),
            "stress": {"disable_real_estate_transfers": True},
        })
    if active_owned_properties:
        presets.append({
            "label": "Estate: sell property now",
            "detail": "Sells currently owned real estate in the projection start month using each property's sale assumptions.",
            "scenario": SimpleNamespace(
                name="Estate: sell property now",
                is_active=True,
                is_preset=True,
                detail="Forces currently owned real estate to sell at the projection start.",
                liquid_balance_delta=Decimal("0.00"),
                monthly_income_delta=Decimal("0.00"),
                monthly_expense_delta=Decimal("0.00"),
            ),
            "stress": {"sell_real_estate_month": household.start_month},
        })
    if active_family_gifts:
        presets.append({
            "label": "Estate: keep cash gifts",
            "detail": "Disables planned family gifts so cash/depot gifts remain in household net worth.",
            "scenario": SimpleNamespace(
                name="Estate: keep cash gifts",
                is_active=True,
                is_preset=True,
                detail="Disables planned family gifts for comparison.",
                liquid_balance_delta=Decimal("0.00"),
                monthly_income_delta=Decimal("0.00"),
                monthly_expense_delta=Decimal("0.00"),
            ),
            "stress": {"disable_family_gifts": True},
        })
    if active_property_transfers and active_family_gifts:
        presets.append({
            "label": "Estate: no estate transfers",
            "detail": "Disables both real-estate transfers and family gifts for a keep-everything comparison.",
            "scenario": SimpleNamespace(
                name="Estate: no estate transfers",
                is_active=True,
                is_preset=True,
                detail="Disables all planned estate transfers for comparison.",
                liquid_balance_delta=Decimal("0.00"),
                monthly_income_delta=Decimal("0.00"),
                monthly_expense_delta=Decimal("0.00"),
            ),
            "stress": {"disable_real_estate_transfers": True, "disable_family_gifts": True},
        })
    return presets


def build_scenario_comparison(household):
    cash_goals = household.cash_goals.all()
    annual_inflation_rate = household.annual_inflation_rate
    rows = []
    base_projection = build_projection(household)
    presets = stress_preset_definitions(household)
    rows.append(
        scenario_outcome(
            "Base plan",
            None,
            base_projection,
            build_yearly_projection(base_projection, cash_goals),
            household.currency,
            annual_inflation_rate,
            household,
        )
    )
    for preset in presets:
        projection = build_projection(household, scenario=preset["scenario"], stress=preset["stress"])
        rows.append(
            scenario_outcome(
                preset["label"],
                preset["scenario"],
                projection,
                build_yearly_projection(projection, cash_goals),
                household.currency,
                annual_inflation_rate,
                household,
            )
        )
    for scenario in household.scenarios.filter(is_active=True):
        projection = build_projection(household, scenario=scenario)
        rows.append(
            scenario_outcome(
                scenario.name,
                scenario,
                projection,
                build_yearly_projection(projection, cash_goals),
                household.currency,
                annual_inflation_rate,
                household,
            )
        )
    rows = [row for row in rows if row]
    if rows:
        base_row = rows[0]
        for row in rows:
            row["ending_liquid_delta"] = row["ending_liquid"] - base_row["ending_liquid"]
            row["ending_net_worth_delta"] = row["ending_net_worth"] - base_row["ending_net_worth"]
            row["ending_depot_delta"] = row["ending_depot"] - base_row["ending_depot"]
            row["ending_liability_delta"] = row["ending_liability"] - base_row["ending_liability"]
            row["lowest_liquid_delta"] = row["lowest_liquid"] - base_row["lowest_liquid"]
            row["ending_cash_goal_gap_delta"] = row["ending_cash_goal_gap"] - base_row["ending_cash_goal_gap"]
            row["ending_tax_aware_draw_need_delta"] = (
                row["ending_tax_aware_draw_need"] - base_row["ending_tax_aware_draw_need"]
            )
            row["stress_months_delta"] = row["stress_months"] - base_row["stress_months"]
    estate_rows = [row for row in rows if row["label"].startswith("Estate:")]
    return {
        "rows": rows,
        "decision_rows": rows[1:5],
        "estate_rows": estate_rows,
        "highlights": scenario_highlights(rows),
        "preset_count": len(presets),
        "chart": {
            "currency": household.currency,
            "inflation": {"annualRate": money_value(annual_inflation_rate)},
            "labels": rows[0]["series"] if rows else [],
            "scenarios": [
                {
                    "label": row["label"],
                    "liquidBalance": [point["liquidBalance"] for point in row["series"]],
                    "liquidBalanceReal": [point["liquidBalanceReal"] for point in row["series"]],
                    "netWorth": [point["netWorth"] for point in row["series"]],
                    "netWorthReal": [point["netWorthReal"] for point in row["series"]],
                    "depotValue": [point["depotValue"] for point in row["series"]],
                    "depotValueReal": [point["depotValueReal"] for point in row["series"]],
                    "liabilityBalance": [point["liabilityBalance"] for point in row["series"]],
                    "liabilityBalanceReal": [point["liabilityBalanceReal"] for point in row["series"]],
                    "annualCashGoal": [point["annualCashGoal"] for point in row["series"]],
                    "annualCashGoalReal": [point["annualCashGoalReal"] for point in row["series"]],
                    "netIncome": [point["netIncome"] for point in row["series"]],
                    "netIncomeReal": [point["netIncomeReal"] for point in row["series"]],
                    "taxAwareDrawNeed": [point["taxAwareDrawNeed"] for point in row["series"]],
                    "taxAwareDrawNeedReal": [point["taxAwareDrawNeedReal"] for point in row["series"]],
                    "portfolioDrawPercent": [point["portfolioDrawPercent"] for point in row["series"]],
                    "taxAwareDrawPercent": [point["taxAwareDrawPercent"] for point in row["series"]],
                }
                for row in rows
            ],
        },
    }


def scenario_highlights(rows):
    if not rows:
        return []
    highlights = [
        {
            "label": "Most liquid ending",
            "detail": "Highest projected liquid balance at the end of the horizon.",
            "winner": max(rows, key=lambda row: row["ending_liquid"]),
            "metric": "ending_liquid",
            "kind": "money",
        },
        {
            "label": "Highest net worth",
            "detail": "Highest projected household net worth at the end of the horizon.",
            "winner": max(rows, key=lambda row: row["ending_net_worth"]),
            "metric": "ending_net_worth",
            "kind": "money",
        },
        {
            "label": "Safest low point",
            "detail": "Best worst-month liquid balance across the whole projection.",
            "winner": max(rows, key=lambda row: row["lowest_liquid"]),
            "metric": "lowest_liquid",
            "kind": "money",
        },
        {
            "label": "Fewest stress months",
            "detail": "Lowest number of months with negative liquidity while still solvent.",
            "winner": min(rows, key=lambda row: row["stress_months"]),
            "metric": "stress_months",
            "kind": "count",
        },
    ]
    if any(row["ending_tax_aware_draw_need"] > 0 for row in rows):
        highlights.append(
            {
                "label": "Lowest retirement draw need",
                "detail": "Lowest tax-aware ETF draw need in the final projected year.",
                "winner": min(rows, key=lambda row: row["ending_tax_aware_draw_need"]),
                "metric": "ending_tax_aware_draw_need",
                "kind": "money",
            }
        )
    base_row = rows[0]
    for item in highlights:
        winner = item["winner"]
        metric = item["metric"]
        item["winner_label"] = winner["label"]
        item["value"] = winner[metric]
        item["delta_vs_base"] = winner[metric] - base_row[metric] if item["kind"] == "money" else winner[metric] - base_row[metric]
        item["is_base"] = winner is base_row
    return highlights


# Audit-line sections that represent income (positive cash inflow that counts
# toward `income`). These must match the section labels emitted by the income
# contributors in projections.py; a mismatch is caught by the reconciliation
# flag in build_income_timeline and its test.
INCOME_AUDIT_SECTIONS = [
    "Income rule",
    "Investment income",
    "Private loan interest",
    "Savings interest",
    "Depot distribution",
    "Retirement income",
    "Equity income",
    "Salary change",
    "Child income",
    "Scenario income",
]


def build_income_timeline(household):
    """Year-by-year income broken out by source, derived from the projection's
    audit lines so every row reconciles to that year's total income by
    construction (income rules included). ``reconciles`` flags any year whose
    per-source sum drifts from the computed income total."""
    months = build_projection(household)
    years = build_yearly_projection(months, household.cash_goals.all())
    milestones_by_year = {}
    for milestone in build_analytics_milestones(household):
        milestones_by_year.setdefault(int(milestone["year"]), []).append(milestone)

    present = []
    raw_rows = []
    for index, year in enumerate(years):
        by_section = {section: Decimal("0.00") for section in INCOME_AUDIT_SECTIONS}
        for line in year.audit_lines:
            if line.section in by_section:
                by_section[line.section] += line.amount
        for section, value in by_section.items():
            if value and section not in present:
                present.append(section)
        raw_rows.append((index, year, by_section))

    columns = [section for section in INCOME_AUDIT_SECTIONS if section in present]
    rows = []
    for index, year, by_section in raw_rows:
        computed_total = sum(by_section.values(), Decimal("0.00"))
        rows.append({
            "label": year.label,
            "year": year.year,
            "detail_url": reverse("planner:projection_year_audit", args=[index]),
            "values": [by_section[section] for section in columns],
            "total": year.income,
            "reconciles": computed_total == year.income,
            "events": milestones_by_year.get(year.year, []),
        })

    column_totals = [
        sum((by_section[section] for _index, _year, by_section in raw_rows), Decimal("0.00"))
        for section in columns
    ]
    return {
        "columns": columns,
        "rows": rows,
        "column_totals": column_totals,
        "grand_total": sum((year.income for year in years), Decimal("0.00")),
        "all_reconcile": all(row["reconciles"] for row in rows),
    }
