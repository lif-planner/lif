from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .models import AssetAccount, MoneyRule

# Household-level fields that materially change the forecast. Kept alongside
# build_assumption_registry (rather than duplicated in views.py) so the two
# can't silently drift apart -- if you add a field to one, add it here too.
HOUSEHOLD_ASSUMPTION_FIELDS = {
    "planning_months",
    "planning_years",
    "annual_inflation_rate",
    "default_income_growth_rate",
    "pension_tax_rate",
    "income_tax_rate",
    "capital_gains_tax_rate",
    "capital_income_allowance",
    "vorabpauschale_basiszins_rate",
    "church_tax_rate",
    "solidarity_surcharge_rate",
    "health_insurance_rate",
    "fund_cash_goal_from_depot",
    "emergency_fund_months",
}


DEFAULT_HOUSEHOLD_ASSUMPTIONS = {
    "annual_inflation_rate": Decimal("2.00"),
    "default_income_growth_rate": Decimal("0.00"),
    "pension_tax_rate": Decimal("18.00"),
    "income_tax_rate": Decimal("0.00"),
    "capital_gains_tax_rate": Decimal("25.00"),
    "capital_income_allowance": Decimal("2000.00"),
    "vorabpauschale_basiszins_rate": Decimal("3.20"),
    "church_tax_rate": Decimal("0.00"),
    "solidarity_surcharge_rate": Decimal("0.00"),
    "health_insurance_rate": Decimal("11.00"),
    "emergency_fund_months": Decimal("0.00"),
}
ASSUMPTION_REVIEW_EXPIRY_DAYS = 365


def _confidence(status, detail):
    labels = {
        "reviewed": "Reviewed",
        "default": "Default",
        "inherited": "Inherited",
        "expired": "Expired",
    }
    classes = {
        "reviewed": "ok",
        "default": "warning",
        "inherited": "info",
        "expired": "warning",
    }
    return {
        "status": status,
        "label": labels[status],
        "class": classes[status],
        "detail": detail,
    }


def _household_confidence(household, field_name, reviewed_detail, default_detail):
    value = getattr(household, field_name)
    if value == DEFAULT_HOUSEHOLD_ASSUMPTIONS[field_name]:
        return _confidence("default", default_detail)
    return _confidence("reviewed", reviewed_detail)


def _row(label, value, detail="", url="", confidence=None, impact=""):
    return {
        "label": label,
        "value": value,
        "detail": detail,
        "url": url,
        "confidence": confidence or _confidence("reviewed", "Explicitly configured item."),
        "impact": impact,
    }


def _review_key(group_key, label):
    return f"{group_key}:{slugify(label) or 'assumption'}"


def _review_age_days(review):
    return (timezone.localdate() - review.reviewed_at.date()).days


def _apply_reviews(groups, reviews_by_key):
    for group in groups:
        for row in group["rows"]:
            key = _review_key(group["key"], row["label"])
            row["review_key"] = key
            review = reviews_by_key.get(key)
            row["review"] = review
            row["review_expired"] = False
            if not review:
                continue
            age_days = _review_age_days(review)
            row["review_age_days"] = age_days
            if age_days > ASSUMPTION_REVIEW_EXPIRY_DAYS:
                row["review_expired"] = True
                row["confidence"] = _confidence(
                    "expired",
                    f"Last reviewed {age_days} days ago; review again for long-range planning.",
                )
            else:
                row["confidence"] = _confidence(
                    "reviewed",
                    f"Reviewed {age_days} days ago" + (f" by {review.reviewed_by}." if review.reviewed_by else "."),
                )
    return groups


def _percent(value):
    return f"{value}%"


def _money(value, currency):
    return f"{value:,.2f} {currency}"


def build_assumption_registry(household, reviews=None):
    currency = household.currency
    horizon_months = household.planning_years * 12 if household.planning_years else household.planning_months
    groups = [
        {
            "key": "household",
            "label": "Household defaults",
            "rows": [
                _row(
                    "Planning horizon",
                    f"{horizon_months} months",
                    "Driven by planning years or planning months.",
                    reverse("planner:household_settings"),
                    _confidence("reviewed", "Planning horizon has been set for this household."),
                    "Changes how far retirement, debt payoff, depot drawdown, and child milestones are projected.",
                ),
                _row(
                    "Inflation",
                    _percent(household.annual_inflation_rate),
                    "Used for today's-money views and indexed cash goals.",
                    reverse("planner:household_settings"),
                    _household_confidence(household, "annual_inflation_rate", "Custom household inflation assumption.", "Still using the app default inflation assumption."),
                    "Moves real purchasing-power charts and any cash goals indexed to inflation.",
                ),
                _row(
                    "Default income growth",
                    _percent(household.default_income_growth_rate),
                    "Used by income rules/investments without their own growth rate.",
                    reverse("planner:household_settings"),
                    _household_confidence(household, "default_income_growth_rate", "Custom household income growth default.", "Income growth default is flat unless individual items override it."),
                    "Changes future salary, investment-income, savings capacity, and FIRE gap if individual income rows inherit it.",
                ),
                _row(
                    "Emergency fund target",
                    f"{household.emergency_fund_months} months",
                    "Used by data-quality checks.",
                    reverse("planner:household_settings"),
                    _household_confidence(household, "emergency_fund_months", "Emergency fund target configured.", "Emergency fund target is disabled by default."),
                    "Changes readiness warnings around liquid cash reserves, not the projection cash flow itself.",
                ),
                _row(
                    "FIRE draw rate",
                    "4.00%",
                    "Used as the dashboard safe-withdrawal guide.",
                    "",
                    _confidence("default", "Fixed guide value; review before using as a personal withdrawal rule."),
                    "Changes dashboard interpretation of portfolio independence; it does not move account balances.",
                ),
            ],
        },
        {
            "key": "tax",
            "label": "Tax and deduction assumptions",
            "rows": [
                _row("Income tax", _percent(household.income_tax_rate), "Only applies to income rules marked taxable.", reverse("planner:household_settings"), _household_confidence(household, "income_tax_rate", "Custom income-tax planning rate.", "Default keeps income rules as entered, usually net."), "Reduces taxable income rules and therefore future liquid cash available for saving or spending."),
                _row("Pension tax", _percent(household.pension_tax_rate), "Applied to taxable retirement-plan payout share.", reverse("planner:household_settings"), _household_confidence(household, "pension_tax_rate", "Custom pension-tax planning rate.", "Still using the app default pension-tax assumption."), "Changes net retirement income and the size of any retirement cash shortfall."),
                _row("Health/care insurance", _percent(household.health_insurance_rate), "Applied to retirement-plan health-insurance share.", reverse("planner:household_settings"), _household_confidence(household, "health_insurance_rate", "Custom health/care insurance rate.", "Still using the app default health/care rate."), "Reduces pension and retirement-plan payouts where health-insurance exposure is modeled."),
                _row("Capital gains tax", _percent(household.capital_gains_tax_rate), "Used for depot draws, distributions, and capital income.", reverse("planner:household_settings"), _household_confidence(household, "capital_gains_tax_rate", "Custom capital-tax planning rate.", "Still using the app default capital-tax assumption."), "Changes net ETF/depot withdrawals, distributions, bond interest, and capital-income cash flow."),
                _row("Capital income allowance", _money(household.capital_income_allowance, currency), "Annual allowance before capital tax.", reverse("planner:household_settings"), _household_confidence(household, "capital_income_allowance", "Custom capital-income allowance.", "Still using the app default allowance."), "Changes how much capital income remains tax-free before depot and interest taxes reduce cash."),
                _row("Vorabpauschale Basiszins", _percent(household.vorabpauschale_basiszins_rate), "Planning rate for accumulating funds.", reverse("planner:household_settings"), _household_confidence(household, "vorabpauschale_basiszins_rate", "Custom Basiszins planning rate.", "Still using the app default Basiszins."), "Changes modeled tax drag for accumulating funds when Vorabpauschale is enabled."),
            ],
        },
    ]

    depot_rows = []
    savings_rows = []
    for account in household.accounts.prefetch_related("holdings"):
        if account.account_type == AssetAccount.AccountType.DEPOT:
            if account.uses_holdings_valuation:
                holdings = list(account.holdings.all())
                distributing_count = sum(1 for holding in holdings if holding.annual_distribution_rate > 0)
                distribution_detail = f"{distributing_count} of {len(holdings)} holdings distribute" if holdings else "No holdings yet"
            else:
                distribution_detail = f"Distribution {account.depot_annual_distribution_rate}%"
            depot_rows.append(_row(
                account.name,
                _percent(account.depot_annual_return_rate),
                (
                    f"{distribution_detail}, "
                    f"Teilfreistellung {account.depot_teilfreistellung_rate}%, "
                    f"valuation: {account.get_depot_valuation_display()}."
                ),
                reverse("planner:account_update", args=[account.pk]),
                _confidence(
                    "default" if account.depot_annual_return_rate == Decimal("0.00") else "reviewed",
                    "Depot return is flat until you set an expected return." if account.depot_annual_return_rate == Decimal("0.00") else "Depot return assumption configured on this account.",
                ),
                "Changes projected invested balance, FIRE readiness, retirement drawdown, and future capital-tax exposure.",
            ))
        if account.account_type == AssetAccount.AccountType.SAVINGS:
            savings_rows.append(_row(
                account.name,
                _percent(account.savings_annual_interest_rate),
                f"Paid {account.get_savings_interest_cadence_display().lower()}, taxed at {account.savings_interest_tax_rate}%.",
                reverse("planner:account_update", args=[account.pk]),
                _confidence(
                    "default" if account.savings_annual_interest_rate == Decimal("0.00") else "reviewed",
                    "Savings interest is flat until you set a rate." if account.savings_annual_interest_rate == Decimal("0.00") else "Savings interest assumption configured on this account.",
                ),
                "Changes liquid cash growth and taxable interest income for this savings account.",
            ))
    if depot_rows:
        groups.append({"key": "depot", "label": "Depot assumptions", "rows": depot_rows})
    if savings_rows:
        groups.append({"key": "savings", "label": "Savings assumptions", "rows": savings_rows})

    debt_rows = [
        _row(
            debt.name,
            _percent(debt.annual_interest_rate),
            (
                f"Payment {debt.monthly_payment} {currency}/mo"
                + (f", fixed until {debt.fixed_interest_until}" if debt.fixed_interest_until else "")
                + (
                    f", refinance {debt.refinance_annual_interest_rate}% / {debt.refinance_monthly_payment} {currency}/mo"
                    if debt.refinance_annual_interest_rate is not None and debt.refinance_monthly_payment is not None
                    else ""
                )
            ),
            reverse("planner:debt_update", args=[debt.pk]),
            _confidence("reviewed", "Debt assumptions are explicitly configured on this debt plan."),
            "Changes monthly cash drain, interest cost, debt payoff timing, and refinance risk.",
        )
        for debt in household.debts.filter(is_active=True)
    ]
    if debt_rows:
        groups.append({"key": "debt", "label": "Debt and refinance assumptions", "rows": debt_rows})

    income_rows = [
        _row(
            rule.name,
            _percent(rule.annual_growth_rate if rule.annual_growth_rate is not None else household.default_income_growth_rate),
            "Own growth rate." if rule.annual_growth_rate is not None else "Uses household default income growth.",
            reverse("planner:rule_update", args=[rule.pk]),
            _confidence(
                "reviewed" if rule.annual_growth_rate is not None else "inherited",
                "Income rule has its own growth rate." if rule.annual_growth_rate is not None else "Uses the household default income growth assumption.",
            ),
            "Changes future household cash inflow, savings capacity, and any ETF/debt transfers funded by surplus cash.",
        )
        for rule in household.rules.filter(kind=MoneyRule.Kind.INCOME, is_active=True)
    ]
    income_rows.extend(
        _row(
            investment.name,
            _percent(investment.annual_growth_rate if investment.annual_growth_rate is not None else household.default_income_growth_rate),
            f"Monthly income {investment.monthly_income} {investment.currency}.",
            reverse("planner:income_investment_update", args=[investment.pk]),
            _confidence(
                "reviewed" if investment.annual_growth_rate is not None else "inherited",
                "Income investment has its own growth rate." if investment.annual_growth_rate is not None else "Uses the household default income growth assumption.",
            ),
            "Changes dated investment income and the cash available during this investment's active period.",
        )
        for investment in household.income_investments.filter(is_active=True)
    )
    if income_rows:
        groups.append({"key": "income", "label": "Income growth assumptions", "rows": income_rows})

    cash_goal_rows = [
        _row(
            goal.name,
            _money(goal.annual_amount, currency),
            (
                f"Starts {goal.start_year}"
                + (f", ends {goal.end_year}" if goal.end_year else "")
                + (", indexed to inflation" if goal.indexed_to_inflation else ", flat nominal amount")
            ),
            reverse("planner:cash_goal_update", args=[goal.pk]),
            _confidence("reviewed", "Cash goal amount and indexing are explicitly configured."),
            "Changes FIRE draw needs, annual spending coverage, and how much cash must come from depot withdrawals.",
        )
        for goal in household.cash_goals.filter(is_active=True)
    ]
    if cash_goal_rows:
        groups.append({"key": "cash_goals", "label": "Cash goal assumptions", "rows": cash_goal_rows})

    retirement_rows = [
        _row(
            plan.name,
            _percent(plan.annual_adjustment_rate),
            (
                f"Retires {plan.retirement_start_month}, points {plan.current_pension_points}, "
                f"expected annual points {plan.expected_annual_points}."
            ),
            reverse("planner:retirement_plan_update", args=[plan.pk]),
            _confidence(
                "default" if plan.annual_adjustment_rate == Decimal("0.00") else "reviewed",
                "Retirement payout adjustment is flat until you set an adjustment rate." if plan.annual_adjustment_rate == Decimal("0.00") else "Retirement adjustment configured on this plan.",
            ),
            "Changes net retirement income, pension gap years, and the need for portfolio withdrawals.",
        )
        for plan in household.retirement_plans.filter(is_active=True).select_related("person")
    ]
    if retirement_rows:
        groups.append({"key": "retirement", "label": "Retirement assumptions", "rows": retirement_rows})

    reviews_by_key = {review.key: review for review in reviews} if reviews is not None else {}
    groups = _apply_reviews(groups, reviews_by_key)
    confidence_counts = {"reviewed": 0, "default": 0, "inherited": 0, "expired": 0}
    for group in groups:
        for row in group["rows"]:
            confidence_counts[row["confidence"]["status"]] += 1
    expired_rows = [
        row
        for group in groups
        for row in group["rows"]
        if row["confidence"]["status"] == "expired"
    ]
    return {
        "groups": groups,
        "assumption_count": sum(len(group["rows"]) for group in groups),
        "confidence_counts": confidence_counts,
        "expired_rows": expired_rows,
        "review_expiry_days": ASSUMPTION_REVIEW_EXPIRY_DAYS,
        "fund_cash_goal_from_depot": household.fund_cash_goal_from_depot,
    }


def build_assumption_review_center(registry):
    queues = [
        {
            "key": "expired",
            "label": "Expired reviews",
            "description": "Reviewed before, but the review is older than the current review window.",
            "rows": [],
            "priority": 1,
        },
        {
            "key": "default",
            "label": "Still using defaults",
            "description": "Values that still use app defaults or flat assumptions and should be confirmed for real planning.",
            "rows": [],
            "priority": 2,
        },
        {
            "key": "inherited",
            "label": "Inherited assumptions",
            "description": "Items using household-level defaults instead of their own explicit assumption.",
            "rows": [],
            "priority": 3,
        },
        {
            "key": "reviewed",
            "label": "Reviewed",
            "description": "Assumptions that currently have explicit or fresh review confidence.",
            "rows": [],
            "priority": 4,
        },
    ]
    queues_by_key = {queue["key"]: queue for queue in queues}

    for group in registry["groups"]:
        for row in group["rows"]:
            status = row["confidence"]["status"]
            queues_by_key[status]["rows"].append(
                {
                    **row,
                    "group_key": group["key"],
                    "group_label": group["label"],
                }
            )

    action_rows = [
        row
        for queue in queues
        if queue["key"] != "reviewed"
        for row in queue["rows"]
    ]
    return {
        "queues": queues,
        "action_rows": action_rows,
        "action_count": len(action_rows),
        "reviewed_count": len(queues_by_key["reviewed"]["rows"]),
        "review_expiry_days": registry["review_expiry_days"],
        "confidence_counts": registry["confidence_counts"],
    }
