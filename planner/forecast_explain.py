from decimal import Decimal


GROUPS = [
    {
        "key": "work_income",
        "title": "Work and recurring income",
        "description": "Salary, benefits, child income, and scenario income that add cash.",
        "sections": {"Income rule", "Salary change", "Child income", "Scenario income"},
    },
    {
        "key": "investment_income",
        "title": "Investment and asset income",
        "description": (
            "Cash created by investments, depot distributions, savings interest, "
            "pensions, RSUs, and private-loan interest."
        ),
        "sections": {
            "Investment income",
            "Savings interest",
            "Depot distribution",
            "Retirement income",
            "Equity income",
            "Private loan interest",
            "Rental income",
        },
    },
    {
        "key": "asset_payouts",
        "title": "Asset payouts and principal returns",
        "description": "Cash returned from assets, such as bond maturities or private-loan principal repayments.",
        "sections": {"Depot payout", "Private loan principal", "Depot draw", "Property sale"},
    },
    {
        "key": "costs",
        "title": "Costs and debt interest",
        "description": (
            "Spending, one-off true expenses, child costs, scenario costs, "
            "and the interest portion of debt payments."
        ),
        "sections": {"Expense rule", "True expense", "Child cost", "Scenario expense", "Debt", "Cash goal spending", "Private loan gift", "Property costs"},
    },
    {
        "key": "transfers",
        "title": "Transfers and balance moves",
        "description": "Cash moved into depots, savings, or debt principal. These change account balances but are not income.",
        "sections": {"Transfer", "Extra repayment", "Investment purchase", "Private loan disbursed", "Property purchase"},
    },
    {
        "key": "valuation",
        "title": "Valuation changes",
        "description": "Non-cash changes to invested assets, such as assumed depot growth.",
        "sections": {"Depot growth", "Property appreciation"},
    },
]

GROUP_BY_SECTION = {
    section: group
    for group in GROUPS
    for section in group["sections"]
}


def _empty_group(group):
    return {
        "key": group["key"],
        "title": group["title"],
        "description": group["description"],
        "amount": Decimal("0.00"),
        "cash_effect": Decimal("0.00"),
        "invested_effect": Decimal("0.00"),
        "other_asset_effect": Decimal("0.00"),
        "liability_effect": Decimal("0.00"),
        "lines": [],
    }


def grouped_audit_lines(audit_lines):
    groups = {group["key"]: _empty_group(group) for group in GROUPS}
    other = _empty_group({
        "key": "other",
        "title": "Other calculation lines",
        "description": "Lines that are not yet assigned to a forecast explanation group.",
    })

    for line in audit_lines:
        group_definition = GROUP_BY_SECTION.get(line.section)
        group = groups[group_definition["key"]] if group_definition else other
        group["amount"] += line.amount
        group["cash_effect"] += line.cash_effect
        group["invested_effect"] += line.invested_effect
        group["other_asset_effect"] += line.other_asset_effect
        group["liability_effect"] += line.liability_effect
        group["lines"].append(line)

    result = [group for group in groups.values() if group["lines"]]
    if other["lines"]:
        result.append(other)
    return result


def _period_value(period, month_name, year_name=None):
    if hasattr(period, month_name):
        return getattr(period, month_name)
    return getattr(period, year_name or month_name)


def _top_lines(lines, effect_name, limit=5):
    return sorted(
        [line for line in lines if getattr(line, effect_name)],
        key=lambda line: abs(getattr(line, effect_name)),
        reverse=True,
    )[:limit]


def _driver_row(line, effect_name):
    return {
        "section": line.section,
        "name": line.name,
        "note": line.note,
        "amount": line.amount,
        "cash_effect": line.cash_effect,
        "invested_effect": line.invested_effect,
        "other_asset_effect": line.other_asset_effect,
        "liability_effect": line.liability_effect,
        "primary_effect": getattr(line, effect_name),
    }


def _unique_rows(lines, effect_name, limit=5):
    seen = set()
    rows = []
    for line in lines:
        key = (line.section, line.name, line.note)
        if key in seen:
            continue
        seen.add(key)
        rows.append(_driver_row(line, effect_name))
        if len(rows) >= limit:
            break
    return rows


def forecast_driver_summary(period):
    audit_lines = getattr(period, "audit_lines", [])
    event_sections = {
        "Cash goal spending",
        "Depot payout",
        "Depot draw",
        "Debt",
        "Equity income",
        "Extra repayment",
        "Investment purchase",
        "Private loan disbursed",
        "Private loan principal",
        "Property purchase",
        "Property sale",
        "Real estate transfer",
        "Retirement income",
        "Salary change",
        "Transfer",
    }
    top_cash = _unique_rows(_top_lines(audit_lines, "cash_effect", limit=8), "cash_effect")
    event_lines = sorted(
        [line for line in audit_lines if line.section in event_sections],
        key=lambda line: (
            abs(line.cash_effect) + abs(line.invested_effect) + abs(line.other_asset_effect) + abs(line.liability_effect),
            abs(line.amount),
        ),
        reverse=True,
    )
    balance_lines = sorted(
        [
            line
            for line in audit_lines
            if line.invested_effect or line.other_asset_effect or line.liability_effect
        ],
        key=lambda line: abs(line.invested_effect) + abs(line.other_asset_effect) + abs(line.liability_effect),
        reverse=True,
    )
    return {
        "groups": [
            {
                "key": "cash",
                "label": "Major cash movements",
                "description": "Largest income, spending, transfer, and payout lines in this period.",
                "rows": top_cash,
            },
            {
                "key": "events",
                "label": "Milestones and planned events",
                "description": "Retirement starts, debt payments, transfers, bond payouts, gifts, property moves, and planned purchases.",
                "rows": _unique_rows(event_lines, "cash_effect"),
            },
            {
                "key": "balances",
                "label": "Balance-sheet movements",
                "description": "Lines that move depot value, real-estate value, private loans, or liabilities.",
                "rows": _unique_rows(balance_lines, "amount"),
            },
        ],
    }


def forecast_explanation_summary(period, groups):
    audit_lines = getattr(period, "audit_lines", [])
    opening_liquid = _period_value(period, "opening_liquid_balance")
    ending_liquid = _period_value(period, "liquid_balance", "ending_liquid_balance")
    opening_net_worth = _period_value(period, "opening_net_worth")
    ending_net_worth = _period_value(period, "net_worth", "ending_net_worth")
    opening_depot = _period_value(period, "opening_invested_balance")
    ending_depot = _period_value(period, "invested_balance", "ending_invested_balance")
    opening_liability = _period_value(period, "opening_liability_balance")
    ending_liability = _period_value(period, "liability_balance", "ending_liability_balance")

    return {
        "cash_bridge": [
            {"label": "Opening liquid", "amount": opening_liquid},
            {"label": "Income", "amount": getattr(period, "income", Decimal("0.00"))},
            {"label": "Expenses", "amount": -getattr(period, "expenses", Decimal("0.00"))},
            {"label": "Transfers", "amount": -getattr(period, "transfers", Decimal("0.00"))},
            {"label": "Ending liquid", "amount": ending_liquid},
        ],
        "balance_changes": [
            {"label": "Liquid change", "amount": ending_liquid - opening_liquid},
            {"label": "Depot change", "amount": ending_depot - opening_depot},
            {"label": "Liability change", "amount": ending_liability - opening_liability},
            {"label": "Net worth change", "amount": ending_net_worth - opening_net_worth},
        ],
        "top_cash_lines": _top_lines(audit_lines, "cash_effect"),
        "top_depot_lines": _top_lines(audit_lines, "invested_effect"),
        "top_liability_lines": _top_lines(audit_lines, "liability_effect"),
    }


def account_ledger_rows(projection, account, year=None):
    """Per-account statement rows (one per attributed movement) with the account's
    running month-end balance. Pass ``year`` to page to a single calendar year; the
    running balance still tracks across the full horizon so it stays correct."""
    rows = []
    opening_balance = account.effective_balance
    for month in projection:
        ending_balance = month.account_balances.get(account.id)
        if ending_balance is None:
            continue
        in_year = year is None or month.month.year == year
        month_rows = []
        for line in month.audit_lines:
            for effect in line.account_effects:
                if effect["account_id"] != account.id:
                    continue
                month_rows.append({
                    "month": month.month,
                    "index": month.index,
                    "section": line.section,
                    "name": line.name,
                    "amount": effect["amount"],
                    "note": line.note,
                    "ending_balance": ending_balance,
                })
        if in_year:
            if month_rows:
                rows.extend(month_rows)
            elif ending_balance != opening_balance:
                rows.append({
                    "month": month.month,
                    "index": month.index,
                    "section": "Balance movement",
                    "name": "Unattributed account movement",
                    "amount": ending_balance - opening_balance,
                    "note": "The account balance changed, but no detailed projection line was attached.",
                    "ending_balance": ending_balance,
                })
        opening_balance = ending_balance
    return rows


def account_ledger_years(projection, account):
    """Calendar years in which the account participates in the forecast."""
    return sorted({month.month.year for month in projection if account.id in month.account_balances})


def general_pool_ledger_rows(projection, household, year=None):
    """Statement rows for cash flows that were not routed to a concrete account."""
    rows = []
    has_liquid_accounts = household.accounts.filter(account_type__in=["cash", "savings"]).exists()
    running_balance = Decimal("0.00") if has_liquid_accounts else household.starting_balance
    for row in cash_flow_ledger_rows(projection):
        if row["account_id"] is not None:
            continue
        running_balance += row["amount"]
        if year is not None and row["year"] != year:
            continue
        rows.append({**row, "ending_balance": running_balance})
    return rows


def general_pool_ledger_years(projection):
    return sorted({row["year"] for row in cash_flow_ledger_rows(projection) if row["account_id"] is None})


def cash_flow_ledger_rows(projection):
    """Every cash movement across all accounts, as a flat chronological list for a
    global ledger. Lines that carry ``account_effects`` are split per affected
    account; a line with only a ``cash_effect`` (e.g. a debt payment not yet routed
    to a specific account) is recorded once against the general liquid pool.
    Non-cash lines (e.g. depot growth) are skipped."""
    rows = []
    for month in projection:
        for line in month.audit_lines:
            group = GROUP_BY_SECTION.get(line.section)
            base = {
                "month": month.month,
                "year": month.month.year,
                "index": month.index,
                "section": line.section,
                "group_key": group["key"] if group else "other",
                "group_title": group["title"] if group else "Other",
                "name": line.name,
                "note": line.note,
            }
            if line.account_effects:
                for effect in line.account_effects:
                    rows.append({**base, "amount": effect["amount"], "account_id": effect["account_id"]})
            elif line.cash_effect:
                rows.append({**base, "amount": line.cash_effect, "account_id": None})
    return rows


def _warning(message, detail, severity="warning"):
    return {
        "message": message,
        "detail": detail,
        "severity": severity,
    }


def forecast_warnings(period, household, months=None):
    warnings = []
    if getattr(period, "income", Decimal("0.00")) == 0 and getattr(period, "expenses", Decimal("0.00")) == 0:
        warnings.append(_warning(
            "No cash-flow lines applied",
            "This period only carries balances forward. Check start and end dates if you expected activity.",
            "info",
        ))

    ending_liquid = getattr(period, "liquid_balance", getattr(period, "ending_liquid_balance", Decimal("0.00")))
    lowest_liquid = getattr(period, "lowest_liquid_balance", ending_liquid)
    if lowest_liquid < 0:
        warnings.append(_warning(
            "Liquid cash goes negative",
            "The forecast needs more cash or a planned asset draw before this period ends.",
            "critical",
        ))
    elif ending_liquid < 0:
        warnings.append(_warning(
            "Ending liquid cash is negative",
            "The bridge ends below zero even though the lowest point is not tracked for this period.",
            "critical",
        ))

    cash_goal_gap = getattr(period, "cash_goal_gap", Decimal("0.00"))
    if cash_goal_gap > 0:
        warnings.append(_warning(
            "Cash goal needs portfolio support",
            "Income does not cover the configured cash goal for this year.",
        ))

    warnings.extend(_input_warnings(household))
    if months:
        warnings.extend(_year_warnings(months))
    return warnings


def _input_warnings(household):
    warnings = []
    for debt in household.debts.filter(is_active=True):
        if debt.monthly_payment <= 0:
            warnings.append(_warning(
                f"{debt.name} has no monthly debt payment",
                "Debt interest/principal may not move as expected until a payment is configured.",
            ))
        if debt.fixed_interest_until and not debt.refinance_monthly_payment:
            warnings.append(_warning(
                f"{debt.name} has no next-phase payment",
                (
                    "After the fixed-interest period, the forecast keeps using the current payment "
                    "unless refinance terms are configured."
                ),
                "info",
            ))
    for loan in household.private_loans.filter(is_active=True):
        if not loan.source_account_id:
            warnings.append(_warning(
                f"{loan.name} has no repayment account",
                "Interest and principal go to the general liquid pool until a cash or savings account is selected.",
                "info",
            ))
    return warnings


def _year_warnings(months):
    warnings = []
    negative_months = [month for month in months if month.liquid_balance < 0]
    if negative_months:
        warnings.append(_warning(
            f"{len(negative_months)} month(s) end below zero cash",
            "Open the affected monthly detail rows to see which rules caused the shortfall.",
            "critical",
        ))
    return warnings
