"""Read-only data layer for the LiF MCP server.

Builds JSON-serializable payloads from the existing service layer so an external
LLM can inspect the modeled inputs, the assumptions that drive them, and the
computed projection — and check whether anything fails to conform.

This module has no dependency on the MCP SDK: it is pure functions that the
management command (and tests) call. Access is gated on the ``mcp_server``
feature flag, checked on every tool call.
"""

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal

from .analytics import build_income_timeline
from .feature_flags import feature_enabled
from .households import active_household
from .models import ChildMilestone, DepotHolding, PlannedInvestmentPurchase, SalaryChange
from .projections import aggregate_audit_lines, build_projection, build_yearly_projection
from .quality import build_quality_report
from .retirement import retirement_tax_summary
from .snapshots import build_snapshot_summary


def mcp_access_enabled():
    return feature_enabled("mcp_server")


def resolve_household():
    return active_household(create=False)


# --- serialization helpers -------------------------------------------------

def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _attr(obj, path):
    current = obj
    for part in path.split("."):
        current = getattr(current, part, None)
        if current is None:
            return None
    return current


def _serialize(obj, fields):
    return {field.replace(".", "_"): _jsonable(_attr(obj, field)) for field in fields}


def _serialize_many(queryset, fields):
    return [_serialize(obj, fields) for obj in queryset]


def _row_dict(item):
    data = asdict(item)
    data.pop("audit_lines", None)
    return _jsonable(data)


# --- input sections --------------------------------------------------------

_INPUT_SECTIONS = [
    ("people", lambda h: h.people.all(), ["name", "role", "birth_date"]),
    (
        "accounts",
        lambda h: h.accounts.all(),
        [
            "name", "account_type", "balance", "currency", "source", "institution",
            "as_of_date", "depot_valuation", "depot_annual_return_rate",
            "depot_annual_distribution_rate", "depot_teilfreistellung_rate", "depot_vorabpauschale_enabled",
            "depot_distribution_cadence",
            "savings_annual_interest_rate", "savings_interest_cadence",
            "savings_interest_tax_rate", "moneymoney_account_key",
        ],
    ),
    (
        "depot_holdings",
        lambda h: DepotHolding.objects.filter(asset_account__household=h).select_related("asset_account"),
        ["asset_account.name", "name", "isin", "ticker", "asset_class", "quantity",
         "latest_price", "currency", "as_of_date", "payout_date", "payout_amount",
         "annual_distribution_rate", "distribution_cadence"],
    ),
    (
        "debts",
        lambda h: h.debts.select_related("account", "source_account", "real_estate"),
        [
            "name", "account.name", "source_account.name", "real_estate.name", "current_principal", "annual_interest_rate",
            "monthly_payment", "start_month", "end_month", "fixed_interest_until",
            "refinance_from_month", "refinance_annual_interest_rate",
            "refinance_monthly_payment", "interest_only_until", "annual_extra_payment",
            "extra_payment_month", "is_active",
        ],
    ),
    (
        "money_rules",
        lambda h: h.rules.select_related("person", "account"),
        ["name", "kind", "amount", "cadence", "annual_growth_rate", "is_taxable", "category", "person.name",
         "account.name", "start_month", "end_month", "is_active"],
    ),
    (
        "transfer_rules",
        lambda h: h.transfer_rules.select_related("person", "source_account", "target_account"),
        ["name", "amount", "cadence", "category", "person.name",
         "source_account.name", "target_account.name", "start_month", "end_month", "is_active"],
    ),
    (
        "private_loans",
        lambda h: h.private_loans.select_related("source_account"),
        ["name", "borrower", "current_principal", "annual_interest_rate", "interest_tax_rate", "monthly_interest_income",
         "monthly_principal_repayment", "currency", "source_account.name", "disbursement_month", "start_month", "end_month", "is_gift", "is_active"],
    ),
    (
        "properties",
        lambda h: h.properties.select_related("source_account", "sale_proceeds_account"),
        [
            "name", "use", "current_value", "annual_appreciation_rate", "currency",
            "acquisition_month", "down_payment", "acquisition_costs",
            "source_account.name", "monthly_costs", "saved_monthly_rent", "monthly_rent",
            "vacancy_rate", "rent_tax_rate", "sale_month", "sale_costs_rate",
            "capital_gains_tax_rate", "sale_proceeds_account.name", "is_active",
        ],
    ),
    (
        "retirement_plans",
        lambda h: h.retirement_plans.select_related("person"),
        ["name", "person.name", "vehicle_type", "current_pension_points", "expected_annual_points",
         "pension_value_per_point", "private_monthly_pension", "retirement_start_month",
         "end_month", "annual_adjustment_rate", "monthly_contribution", "contribution_start_month",
         "contribution_end_month", "contribution_relief_rate", "payout_taxable_rate",
         "payout_health_insurance_rate", "is_active"],
    ),
    (
        "equity_grants",
        lambda h: h.equity_grants.select_related("person", "account"),
        ["name", "person.name", "grant_type", "gross_vest_value", "withholding_rate",
         "account.name", "cadence", "first_vest_month", "last_vest_month", "is_active"],
    ),
    (
        "income_investments",
        lambda h: h.income_investments.select_related("source_account"),
        ["name", "investment_type", "principal", "source_account.name", "monthly_income", "annual_growth_rate",
         "start_month", "end_month", "is_active"],
    ),
    (
        "planned_investment_purchases",
        lambda h: PlannedInvestmentPurchase.objects.filter(household=h).select_related("source_account", "target_account"),
        [
            "name", "asset_type", "isin", "ticker", "source_account.name", "target_account.name",
            "purchase_amount", "purchase_month", "payout_date", "payout_amount", "is_active",
        ],
    ),
    (
        "true_expenses",
        lambda h: h.true_expenses.select_related("account"),
        ["name", "category", "account.name", "amount", "cadence", "first_due_month", "end_month", "is_active"],
    ),
    (
        "cash_goals",
        lambda h: h.cash_goals.all(),
        ["name", "annual_amount", "indexed_to_inflation", "start_year", "end_year", "is_active"],
    ),
    (
        "scenarios",
        lambda h: h.scenarios.all(),
        ["name", "liquid_balance_delta", "monthly_income_delta", "monthly_expense_delta", "is_active"],
    ),
    (
        "child_milestones",
        lambda h: ChildMilestone.objects.filter(person__household=h).select_related("person"),
        ["person.name", "name", "start_month", "end_month", "monthly_cost_delta",
         "monthly_income_delta", "is_active"],
    ),
    (
        "salary_changes",
        lambda h: SalaryChange.objects.filter(person__household=h).select_related("person", "account"),
        ["person.name", "name", "account.name", "start_month", "end_month", "monthly_net_income_delta", "is_active"],
    ),
]


def _assumptions(household):
    return {
        "currency": household.currency,
        "start_month": _jsonable(household.start_month),
        "planning_months": household.planning_months,
        "planning_years": household.planning_years,
        "projection_months": household.projection_months,
        "starting_balance": _jsonable(household.starting_balance),
        "annual_inflation_rate": _jsonable(household.annual_inflation_rate),
        "default_income_growth_rate": _jsonable(household.default_income_growth_rate),
        "default_operating_account": household.default_operating_account.name if household.default_operating_account else "",
        "pension_tax_rate": _jsonable(household.pension_tax_rate),
        "income_tax_rate": _jsonable(household.income_tax_rate),
        "capital_gains_tax_rate": _jsonable(household.capital_gains_tax_rate),
        "capital_income_allowance": _jsonable(household.capital_income_allowance),
        "vorabpauschale_basiszins_rate": _jsonable(household.vorabpauschale_basiszins_rate),
        "church_tax_rate": _jsonable(household.church_tax_rate),
        "solidarity_surcharge_rate": _jsonable(household.solidarity_surcharge_rate),
        "health_insurance_rate": _jsonable(household.health_insurance_rate),
        "fund_cash_goal_from_depot": household.fund_cash_goal_from_depot,
        "emergency_fund_months": _jsonable(household.emergency_fund_months),
    }


# --- tool builders ---------------------------------------------------------

def _overview(household, arguments):
    summary = build_snapshot_summary(household)
    quality = build_quality_report(household)
    severity_counts = {}
    for item in quality.get("issues", []):
        severity_counts[item.severity] = severity_counts.get(item.severity, 0) + 1
    yearly = summary.get("projection", {}).get("yearly", [])
    return {
        "household": summary.get("household", {}),
        "assumptions": _assumptions(household),
        "current_totals": summary.get("totals", {}),
        "counts": summary.get("counts", {}),
        "projection_endpoints": {
            "first_year": yearly[0] if yearly else None,
            "last_year": yearly[-1] if yearly else None,
        },
        "quality_severity_counts": severity_counts,
        "guidance": (
            "Read 'assumptions' and 'inputs' first, then 'projection' and "
            "'audit_lines' to verify the computed figures. Differences from an "
            "external model usually trace back to assumptions."
        ),
    }


def _assumptions_tool(household, arguments):
    return {"assumptions": _assumptions(household)}


def _inputs(household, arguments):
    return {section: _serialize_many(accessor(household), fields) for section, accessor, fields in _INPUT_SECTIONS}


def _projection(household, arguments):
    granularity = (arguments.get("granularity") or "yearly").lower()
    months = build_projection(household)
    if granularity == "monthly":
        rows = [_row_dict(item) for item in months]
    else:
        granularity = "yearly"
        years = build_yearly_projection(months, household.cash_goals.all())
        rows = [_row_dict(item) for item in years]
    return {"granularity": granularity, "row_count": len(rows), "rows": rows}


def _audit_lines(household, arguments):
    months = build_projection(household)
    lines = aggregate_audit_lines(months)
    return {"audit_lines": [_jsonable(asdict(line)) for line in lines]}


def _quality(household, arguments):
    report = build_quality_report(household)
    return {
        "total": report.get("total", 0),
        "issues": [_jsonable(asdict(item)) for item in report.get("issues", [])],
    }


def _debt_schedules(household, arguments):
    from .projections import first_of_month, summarize_debt

    start = first_of_month(household.start_month)
    debts = []
    for debt in household.debts.select_related("account"):
        debts.append({
            "name": debt.name,
            "account": debt.account.name,
            "is_active": debt.is_active,
            "current_principal": _jsonable(debt.current_principal),
            "annual_interest_rate": _jsonable(debt.annual_interest_rate),
            "monthly_payment": _jsonable(debt.monthly_payment),
            "summary": _jsonable(summarize_debt(debt, start)),
        })
    return {"debts": debts}


def _retirement_analysis(household, arguments):
    months = build_projection(household)
    years = build_yearly_projection(months, household.cash_goals.all())
    analysis = []
    for item in years:
        if item.retirement_income <= 0:
            continue
        analysis.append({
            "year": item.year,
            "label": item.label,
            "retirement_income": _jsonable(item.retirement_income),
            "annual_cash_goal": _jsonable(item.annual_cash_goal),
            "opening_invested_balance": _jsonable(item.opening_invested_balance),
            "tax_summary": _jsonable(retirement_tax_summary(item, household)),
        })
    return {"retirement_years": analysis}


def _income_timeline(household, arguments):
    return _jsonable(build_income_timeline(household))


_TOOLS = {
    "overview": {
        "fn": _overview,
        "description": "Household, assumptions, current totals, counts, projection endpoints, and a quality summary. Start here.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "assumptions": {
        "fn": _assumptions_tool,
        "description": "All planning assumptions (currency, horizon, inflation, tax and health-insurance rates). Differences from an external model usually start here.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "inputs": {
        "fn": _inputs,
        "description": "Every modeled input: people, accounts, holdings, debts, properties, rules, retirement plans, equity grants, income investments, true expenses, cash goals, scenarios, child milestones, salary changes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "projection": {
        "fn": _projection,
        "description": "The computed projection rows. granularity='yearly' (default) or 'monthly'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "granularity": {"type": "string", "enum": ["yearly", "monthly"], "default": "yearly"},
            },
        },
    },
    "audit_lines": {
        "fn": _audit_lines,
        "description": "Aggregated audit lines explaining every cash/asset/liability effect across the projection — the trail for verifying computed totals.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "quality_report": {
        "fn": _quality,
        "description": "LiF's own conformance/health findings (the things it already flags as off).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "debt_schedules": {
        "fn": _debt_schedules,
        "description": "Per-debt amortization summary: payoff month, months to payoff, lifetime interest, ending principal.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "retirement_analysis": {
        "fn": _retirement_analysis,
        "description": "Per retirement-year tax-aware summary: net income, cash gap, and the gross depot draw needed.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "income_timeline": {
        "fn": _income_timeline,
        "description": "Year-by-year income broken out by source (salary/income rules, investments, savings, retirement, equity, etc.). Each year's sources reconcile to total income.",
        "input_schema": {"type": "object", "properties": {}},
    },
}


def tool_definitions():
    return [
        {"name": name, "description": spec["description"], "input_schema": spec["input_schema"]}
        for name, spec in _TOOLS.items()
    ]


def call_tool(name, arguments=None):
    """Dispatch a read-only tool call. Always returns a JSON-serializable dict;
    enforces the feature flag and a configured household on every call."""
    if not mcp_access_enabled():
        return {"error": "MCP access is disabled. Enable the 'mcp_server' feature flag in LiF."}
    household = resolve_household()
    if household is None:
        return {"error": "No household is configured in LiF."}
    spec = _TOOLS.get(name)
    if spec is None:
        return {"error": f"Unknown tool: {name}"}
    return spec["fn"](household, arguments or {})
