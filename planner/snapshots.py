from collections import Counter
from decimal import Decimal
from datetime import date

from .balance_sheet import current_balance_sheet
from .finance import money_value as money
from .models import (
    CashGoal,
    Debt,
    DepotHolding,
    FamilyGiftPlan,
    MoneyRule,
    PlannedInvestmentPurchase,
    PrivateLoanReceivable,
    RealEstate,
    RealEstateTransferPlan,
    TransferRule,
)
from .projections import build_projection, build_yearly_projection


# Bump when the stored summary shape changes. Comparison stays backward
# compatible with older (and key-less hand-built) summaries.
#   v1: no version, comparable rows diffed by re-derived string keys.
#   v2: schema_version + a stable per-row identity key (the model PK) so diffing
#       joins on identity instead of re-deriving keys at compare time.
SNAPSHOT_SCHEMA_VERSION = 2


def comparable_key(prefix, obj):
    """Stable identity for a comparable row: the model PK, namespaced by type.
    Survives renames and balance changes, and never collides."""
    return f"{prefix}:{obj.pk}"


def date_value(value):
    return value.isoformat() if value else ""


def decimal_value(value):
    if value in (None, ""):
        return Decimal("0.00")
    return Decimal(str(value))


def month_value(value):
    if not value:
        return None
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    return date.fromisoformat(value[:10])


def delta_row(snapshot_value, current_value):
    snapshot_amount = decimal_value(snapshot_value)
    current_amount = decimal_value(current_value)
    delta = current_amount - snapshot_amount
    if delta > 0:
        direction = "positive"
    elif delta < 0:
        direction = "negative"
    else:
        direction = "neutral"
    return {
        "snapshot_value": money(snapshot_amount),
        "current_value": money(current_amount),
        "delta": money(delta),
        "direction": direction,
    }


def account_key(row):
    return (row.get("name") or "").casefold()


def account_discriminator(row):
    # Disambiguates accounts that legitimately share a name (e.g. duplicate
    # MoneyMoney accounts) by their source key when one is recorded.
    return (row.get("source_key") or "").strip().casefold()


def holding_key(row):
    # Include the account so the same ISIN held in two depots stays distinct.
    account = (row.get("account") or "").casefold()
    isin = (row.get("isin") or "").strip().casefold()
    if isin:
        return f"isin:{account}:{isin}"
    return f"holding:{account}:{(row.get('name') or '').casefold()}"


def debt_key(row):
    return (row.get("name") or row.get("account") or "").casefold()


def private_loan_key(row):
    borrower = (row.get("borrower") or "").casefold()
    return f"{(row.get('name') or '').casefold()}|{borrower}"


def property_key(row):
    return (row.get("name") or "").casefold()


def change_status(snapshot_row, current_row, delta):
    if snapshot_row is None:
        return "new"
    if current_row is None:
        return "missing"
    return "changed" if decimal_value(delta) != Decimal("0.00") else "same"


def keyed_rows(rows, key_func, discriminator_func=None):
    # Prefer the stable identity key baked into v2 rows. For legacy/key-less
    # rows, fall back to the derived key, disambiguating repeated base keys so
    # distinct same-named rows do not silently clobber each other.
    base_keys = [row.get("key") or key_func(row) for row in rows]
    counts = Counter(base_keys)
    by_key = {}
    for row, base in zip(rows, base_keys):
        key = base
        if not row.get("key") and discriminator_func and counts[base] > 1:
            discriminator = discriminator_func(row)
            if discriminator:
                key = f"{base}|{discriminator}"
        by_key[key] = row
    return by_key


def compare_rows(snapshot_rows, current_rows, key_func, value_key, label_func, discriminator_func=None):
    snapshot_by_key = keyed_rows(snapshot_rows, key_func, discriminator_func)
    current_by_key = keyed_rows(current_rows, key_func, discriminator_func)
    keys = sorted(set(snapshot_by_key) | set(current_by_key))
    rows = []
    for key in keys:
        snapshot_row = snapshot_by_key.get(key)
        current_row = current_by_key.get(key)
        display_row = current_row or snapshot_row or {}
        value_delta = delta_row(
            snapshot_row.get(value_key) if snapshot_row else "0.00",
            current_row.get(value_key) if current_row else "0.00",
        )
        rows.append(
            {
                "key": key,
                "label": label_func(snapshot_row or current_row),
                "snapshot_row": snapshot_row,
                "current_row": current_row,
                "display_type": display_row.get("type") or "",
                "display_as_of_date": display_row.get("as_of_date") or "",
                "display_isin": display_row.get("isin") or "",
                "display_account": display_row.get("account") or "",
                "display_borrower": display_row.get("borrower") or "",
                "status": change_status(snapshot_row, current_row, value_delta["delta"]),
                **value_delta,
            }
        )
    return rows


# Single source of truth for the comparable snapshot sections: how each is
# identified (fallback key for legacy rows), which value is diffed, how a row is
# labelled, and how same-named legacy rows are disambiguated.
COMPARABLE_SECTIONS = (
    {
        "name": "accounts",
        "key_func": account_key,
        "value_field": "effective_balance",
        "label_func": lambda row: row.get("name") or "Account",
        "discriminator_func": account_discriminator,
    },
    {
        "name": "holdings",
        "key_func": holding_key,
        "value_field": "current_value",
        "label_func": lambda row: row.get("name") or row.get("isin") or "Holding",
        "discriminator_func": None,
    },
    {
        "name": "debts",
        "key_func": debt_key,
        "value_field": "current_principal",
        "label_func": lambda row: row.get("name") or row.get("account") or "Debt",
        "discriminator_func": None,
    },
    {
        "name": "private_loans",
        "key_func": private_loan_key,
        "value_field": "current_principal",
        "label_func": lambda row: row.get("name") or "Private loan",
        "discriminator_func": None,
    },
    {
        "name": "properties",
        "key_func": property_key,
        "value_field": "current_value",
        "label_func": lambda row: row.get("name") or "Property",
        "discriminator_func": None,
    },
)

TOTAL_LABELS = (
    ("liquid", "Liquid"),
    ("invested", "Invested"),
    ("other_assets", "Other assets"),
    ("liabilities", "Liabilities"),
    ("net_worth", "Net worth"),
)
PROJECTION_YEAR_LABELS = (
    ("liquid_balance", "Liquid"),
    ("invested_balance", "Invested"),
    ("liability_balance", "Liabilities"),
    ("net_worth", "Net worth"),
    ("income", "Income"),
    ("expenses", "Expenses"),
    ("net", "Net cash flow"),
)
HOUSEHOLD_DRIVER_FIELDS = (
    ("planning_months", "Planning months"),
    ("planning_years", "Planning years"),
    ("display_granularity", "Display granularity"),
    ("annual_inflation_rate", "Inflation"),
    ("default_income_growth_rate", "Default income growth"),
    ("capital_income_allowance", "Capital income allowance"),
    ("vorabpauschale_basiszins_rate", "Vorabpauschale Basiszins"),
    ("emergency_fund_months", "Emergency fund target"),
    ("default_operating_account", "Default operating account"),
)
GENERIC_DRIVER_SECTIONS = (
    {
        "name": "rules",
        "label": "Income and expense rules",
        "key_fields": ("name", "kind", "category"),
        "value_fields": ("amount", "cadence", "annual_growth_rate", "account", "is_active"),
    },
    {
        "name": "transfer_rules",
        "label": "Transfer rules",
        "key_fields": ("name", "target_account"),
        "value_fields": ("source_account", "amount", "cadence", "category", "is_active"),
    },
    {
        "name": "cash_goals",
        "label": "Cash goals",
        "key_fields": ("name",),
        "value_fields": ("annual_amount", "start_year", "end_year", "is_active"),
    },
    {
        "name": "family_gift_plans",
        "label": "Family gift plans",
        "key_fields": ("key", "name"),
        "value_fields": ("giver", "recipient", "source_account", "target_account", "amount", "gift_month", "is_active"),
    },
    {
        "name": "planned_investment_purchases",
        "label": "Planned investment purchases",
        "key_fields": ("key", "name"),
        "value_fields": ("asset_type", "isin", "source_account", "target_account", "purchase_amount", "purchase_month", "payout_date", "payout_amount", "is_active"),
    },
    {
        "name": "real_estate_transfer_plans",
        "label": "Real estate transfer plans",
        "key_fields": ("key", "name"),
        "value_fields": ("property", "giver", "recipient", "transfer_month", "ownership_percent", "taxable_gift_value", "retained_niessbrauch", "is_active"),
    },
)


def row_identity(row, fields):
    parts = [str(row.get(field, "")).strip().casefold() for field in fields if row.get(field, "") not in (None, "")]
    return "|".join(parts) or "row"


def changed_field_labels(snapshot_row, current_row, fields):
    labels = []
    for field in fields:
        if str((snapshot_row or {}).get(field, "")) != str((current_row or {}).get(field, "")):
            labels.append(field.replace("_", " "))
    return labels


def generic_driver_rows(snapshot_rows, current_rows, key_fields, value_fields):
    snapshot_by_key = keyed_rows(snapshot_rows, lambda row: row_identity(row, key_fields))
    current_by_key = keyed_rows(current_rows, lambda row: row_identity(row, key_fields))
    rows = []
    for key in sorted(set(snapshot_by_key) | set(current_by_key)):
        snapshot_row = snapshot_by_key.get(key)
        current_row = current_by_key.get(key)
        display_row = current_row or snapshot_row or {}
        label = display_row.get("name") or display_row.get("target_account") or display_row.get("property") or key
        if snapshot_row is None:
            status = "new"
            detail = "New item since snapshot."
        elif current_row is None:
            status = "missing"
            detail = "Removed since snapshot."
        else:
            changed_fields = changed_field_labels(snapshot_row, current_row, value_fields)
            status = "changed" if changed_fields else "same"
            detail = "Changed: " + ", ".join(changed_fields[:6]) if changed_fields else "No tracked fields changed."
        rows.append({"label": label, "status": status, "detail": detail})
    return rows


def changed_driver_rows(rows):
    return [row for row in rows if row["status"] != "same"]


def build_projection_change_drivers(snapshot_summary, current_summary):
    snapshot_summary = snapshot_summary or {}
    current_summary = current_summary or {}
    snapshot_household = snapshot_summary.get("household", {})
    current_household = current_summary.get("household", {})
    assumption_rows = []
    for key, label in HOUSEHOLD_DRIVER_FIELDS:
        snapshot_value = snapshot_household.get(key, "")
        current_value = current_household.get(key, "")
        if str(snapshot_value) != str(current_value):
            assumption_rows.append(
                {
                    "label": label,
                    "status": "changed",
                    "detail": f"{snapshot_value or '-'} -> {current_value or '-'}",
                }
            )

    foundation_rows = []
    foundation_comparison = compare_snapshot_summaries(snapshot_summary, current_summary)
    for section in ("accounts", "holdings", "debts", "private_loans", "properties"):
        label = section.replace("_", " ").title()
        changed_rows = changed_driver_rows(foundation_comparison.get(section, []))
        if changed_rows:
            foundation_rows.append(
                {
                    "label": label,
                    "status": "changed",
                    "detail": f"{len(changed_rows)} changed/new/removed item(s).",
                }
            )

    planning_rows = []
    for section in GENERIC_DRIVER_SECTIONS:
        rows = generic_driver_rows(
            snapshot_summary.get(section["name"], []),
            current_summary.get(section["name"], []),
            section["key_fields"],
            section["value_fields"],
        )
        for row in changed_driver_rows(rows):
            planning_rows.append(
                {
                    "label": f"{section['label']}: {row['label']}",
                    "status": row["status"],
                    "detail": row["detail"],
                }
            )

    groups = [
        {
            "key": "assumptions",
            "label": "Changed household assumptions",
            "description": "Household-level settings stored in the snapshot versus current settings.",
            "rows": assumption_rows,
        },
        {
            "key": "planning",
            "label": "Changed planning objects",
            "description": "Income, expense, transfer, cash-goal, gift, and planned-purchase inputs that changed.",
            "rows": planning_rows,
        },
        {
            "key": "foundation",
            "label": "Changed balances and assets",
            "description": "Accounts, holdings, debts, private loans, and real estate values that changed.",
            "rows": foundation_rows,
        },
    ]
    return {
        "groups": groups,
        "change_count": sum(len(group["rows"]) for group in groups),
    }


def compare_snapshot_summaries(baseline_summary, comparison_summary):
    baseline_summary = baseline_summary or {}
    comparison_summary = comparison_summary or {}
    baseline_totals = baseline_summary.get("totals", {})
    comparison_totals = comparison_summary.get("totals", {})

    result = {
        "schema_version": comparison_summary.get("schema_version", baseline_summary.get("schema_version", 1)),
        "currency": comparison_summary.get("household", {}).get(
            "currency",
            baseline_summary.get("household", {}).get("currency", "EUR"),
        ),
        "totals": [
            {"key": key, "label": label, **delta_row(baseline_totals.get(key), comparison_totals.get(key))}
            for key, label in TOTAL_LABELS
        ],
    }
    for section in COMPARABLE_SECTIONS:
        result[section["name"]] = compare_rows(
            baseline_summary.get(section["name"], []),
            comparison_summary.get(section["name"], []),
            section["key_func"],
            section["value_field"],
            section["label_func"],
            discriminator_func=section["discriminator_func"],
        )
    return result


def compare_snapshot_to_current(snapshot_summary, current_summary):
    comparison = compare_snapshot_summaries(snapshot_summary, current_summary)
    planned_row = planned_row_for_current_month(snapshot_summary)
    if planned_row:
        totals = current_summary.get("totals", {})
        comparison["planned_current_month"] = {
            "label": planned_row["label"],
            "rows": [
                {
                    "key": "liquid",
                    "label": "Liquid",
                    **delta_row(planned_row.get("liquid_balance"), totals.get("liquid")),
                },
                {
                    "key": "invested",
                    "label": "Invested",
                    **delta_row(planned_row.get("invested_balance"), totals.get("invested")),
                },
                {
                    "key": "liabilities",
                    "label": "Liabilities",
                    **delta_row(planned_row.get("liability_balance"), totals.get("liabilities")),
                },
                {
                    "key": "net_worth",
                    "label": "Net worth",
                    **delta_row(planned_row.get("net_worth"), totals.get("net_worth")),
                },
            ],
        }
    return comparison


def compare_projection_summaries(snapshot_summary, current_summary):
    snapshot_summary = snapshot_summary or {}
    current_summary = current_summary or {}
    snapshot_years = {
        row.get("year"): row
        for row in snapshot_summary.get("projection", {}).get("yearly", [])
        if row.get("year") is not None
    }
    current_years = {
        row.get("year"): row
        for row in current_summary.get("projection", {}).get("yearly", [])
        if row.get("year") is not None
    }
    years = sorted(set(snapshot_years) | set(current_years))
    rows = []
    for year in years:
        snapshot_row = snapshot_years.get(year)
        current_row = current_years.get(year)
        field_rows = [
            {
                "key": key,
                "label": label,
                **delta_row(
                    snapshot_row.get(key) if snapshot_row else "0.00",
                    current_row.get(key) if current_row else "0.00",
                ),
            }
            for key, label in PROJECTION_YEAR_LABELS
        ]
        changed = any(field["direction"] != "neutral" for field in field_rows)
        if snapshot_row is None:
            status = "new"
        elif current_row is None:
            status = "missing"
        elif changed:
            status = "changed"
        else:
            status = "same"
        rows.append(
            {
                "year": year,
                "label": (current_row or snapshot_row or {}).get("label") or str(year),
                "status": status,
                "fields": field_rows,
            }
        )

    changed_rows = [row for row in rows if row["status"] != "same"]
    endpoint = rows[-1] if rows else None
    return {
        "currency": current_summary.get("household", {}).get(
            "currency",
            snapshot_summary.get("household", {}).get("currency", "EUR"),
        ),
        "rows": rows,
        "changed_rows": changed_rows,
        "changed_count": len(changed_rows),
        "endpoint": endpoint,
    }


def planned_row_for_current_month(snapshot_summary):
    projection_rows = (snapshot_summary or {}).get("projection", {}).get("monthly", [])
    if not projection_rows:
        return None
    current = date.today().replace(day=1)
    dated_rows = []
    for row in projection_rows:
        try:
            row_month = month_value(row.get("month"))
        except ValueError:
            continue
        if row_month:
            dated_rows.append((row_month, row))
    if not dated_rows:
        return None
    past_or_current = [item for item in dated_rows if item[0] <= current]
    if past_or_current:
        return max(past_or_current, key=lambda item: item[0])[1]
    return min(dated_rows, key=lambda item: item[0])[1]


def projection_month_summary(item):
    return {
        "index": item.index,
        "month": date_value(item.month),
        "label": item.month.strftime("%b %Y"),
        "liquid_balance": money(item.liquid_balance),
        "invested_balance": money(item.invested_balance),
        "liability_balance": money(item.liability_balance),
        "net_worth": money(item.net_worth),
        "income": money(item.income),
        "expenses": money(item.expenses),
        "net": money(item.net),
    }


def projection_year_summary(item):
    return {
        "year": item.year,
        "label": item.label,
        "liquid_balance": money(item.ending_liquid_balance),
        "invested_balance": money(item.ending_invested_balance),
        "liability_balance": money(item.ending_liability_balance),
        "net_worth": money(item.ending_net_worth),
        "income": money(item.income),
        "expenses": money(item.expenses),
        "net": money(item.net),
    }


def build_snapshot_summary(household):
    accounts = list(household.accounts.prefetch_related("holdings"))
    totals = current_balance_sheet(household, accounts)
    holdings = DepotHolding.objects.filter(asset_account__household=household).select_related("asset_account")
    debts = Debt.objects.filter(household=household).select_related("account", "source_account")
    private_loans = PrivateLoanReceivable.objects.filter(household=household).select_related("source_account")
    properties = RealEstate.objects.filter(household=household).select_related(
        "source_account", "sale_proceeds_account"
    ).prefetch_related("debts")
    property_transfers = RealEstateTransferPlan.objects.filter(household=household).select_related(
        "property_item",
        "giver",
        "recipient",
    )
    rules = MoneyRule.objects.filter(household=household).select_related("account")
    transfer_rules = TransferRule.objects.filter(household=household).select_related("source_account", "target_account")
    family_gifts = FamilyGiftPlan.objects.filter(household=household).select_related(
        "giver",
        "recipient",
        "source_account",
        "target_account",
    )
    planned_purchases = PlannedInvestmentPurchase.objects.filter(household=household).select_related(
        "source_account",
        "target_account",
    )
    cash_goals = CashGoal.objects.filter(household=household)
    projection = build_projection(household)
    yearly_projection = build_yearly_projection(projection, cash_goals)

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "household": {
            "name": household.name,
            "currency": household.currency,
            "start_month": date_value(household.start_month),
            "planning_months": household.planning_months,
            "planning_years": household.planning_years,
            "display_granularity": household.display_granularity,
            "annual_inflation_rate": str(household.annual_inflation_rate),
            "default_income_growth_rate": str(household.default_income_growth_rate),
            "capital_income_allowance": money(household.capital_income_allowance),
            "vorabpauschale_basiszins_rate": str(household.vorabpauschale_basiszins_rate),
            "emergency_fund_months": str(household.emergency_fund_months),
            "default_operating_account": household.default_operating_account.name if household.default_operating_account else "",
        },
        "totals": {
            "liquid": money(totals["liquid_total"]),
            "invested": money(totals["invested_total"]),
            "other_assets": money(totals["other_asset_total"]),
            "liabilities": money(totals["liability_total"]),
            "net_worth": money(totals["net_worth"]),
        },
        "counts": {
            "people": household.people.count(),
            "accounts": len(accounts),
            "holdings": holdings.count(),
            "debts": debts.count(),
            "private_loans": private_loans.count(),
            "properties": properties.count(),
            "real_estate_transfer_plans": property_transfers.count(),
            "rules": rules.count(),
            "transfer_rules": transfer_rules.count(),
            "family_gift_plans": family_gifts.count(),
            "planned_investment_purchases": planned_purchases.count(),
            "cash_goals": cash_goals.count(),
        },
        "accounts": [
            {
                "key": comparable_key("account", account),
                "name": account.name,
                "type": account.account_type,
                "balance": money(account.balance),
                "effective_balance": money(account.effective_balance),
                "currency": account.currency,
                "owner_type": account.owner_type,
                "owner_person": account.owner_person.name if account.owner_person else "",
                "counts_in_household_net_worth": account.counts_in_household_net_worth,
                "as_of_date": date_value(account.as_of_date),
                "source_key": account.moneymoney_account_key,
                "depot_teilfreistellung_rate": str(account.depot_teilfreistellung_rate),
                "depot_vorabpauschale_enabled": account.depot_vorabpauschale_enabled,
            }
            for account in accounts
        ],
        "holdings": [
            {
                "key": comparable_key("holding", holding),
                "account": holding.asset_account.name,
                "name": holding.name,
                "isin": holding.isin,
                "ticker": holding.ticker,
                "asset_class": holding.asset_class,
                "quantity": str(holding.quantity),
                "latest_price": money(holding.latest_price),
                "current_value": money(holding.current_value),
                "currency": holding.currency,
                "as_of_date": date_value(holding.as_of_date),
                "payout_date": date_value(holding.payout_date),
                "payout_amount": money(holding.payout_amount) if holding.payout_amount is not None else "",
            }
            for holding in holdings
        ],
        "debts": [
            {
                "key": comparable_key("debt", debt),
                "name": debt.name,
                "account": debt.account.name,
                "source_account": debt.source_account.name if debt.source_account else "",
                "current_principal": money(debt.current_principal),
                "annual_interest_rate": str(debt.annual_interest_rate),
                "monthly_payment": money(debt.monthly_payment),
                "fixed_interest_until": date_value(debt.fixed_interest_until),
                "is_active": debt.is_active,
            }
            for debt in debts
        ],
        "private_loans": [
            {
                "key": comparable_key("loan", loan),
                "name": loan.name,
                "borrower": loan.borrower,
                "source_account": loan.source_account.name if loan.source_account else "",
                "current_principal": money(loan.current_principal),
                "annual_interest_rate": str(loan.annual_interest_rate),
                "interest_tax_rate": str(loan.interest_tax_rate),
                "monthly_interest_income": money(loan.monthly_interest_income),
                "monthly_principal_repayment": money(loan.monthly_principal_repayment),
                "currency": loan.currency,
                "disbursement_month": date_value(loan.disbursement_month),
                "start_month": date_value(loan.start_month),
                "end_month": date_value(loan.end_month),
                "is_gift": loan.is_gift,
                "is_active": loan.is_active,
            }
            for loan in private_loans
        ],
        "properties": [
            {
                "key": comparable_key("property", property_item),
                "name": property_item.name,
                "type": property_item.use,
                "current_value": money(property_item.current_value),
                "annual_appreciation_rate": str(property_item.annual_appreciation_rate),
                "currency": property_item.currency,
                "acquisition_month": date_value(property_item.acquisition_month),
                "down_payment": money(property_item.down_payment),
                "acquisition_costs": money(property_item.acquisition_costs),
                "source_account": property_item.source_account.name if property_item.source_account else "",
                "mortgages": ", ".join(debt.name for debt in property_item.debts.all()),
                "monthly_costs": money(property_item.monthly_costs),
                "monthly_rent": money(property_item.monthly_rent),
                "vacancy_rate": str(property_item.vacancy_rate),
                "rent_tax_rate": str(property_item.rent_tax_rate),
                "sale_month": date_value(property_item.sale_month),
                "sale_costs_rate": str(property_item.sale_costs_rate),
                "capital_gains_tax_rate": str(property_item.capital_gains_tax_rate),
                "sale_proceeds_account": property_item.sale_proceeds_account.name if property_item.sale_proceeds_account else "",
                "is_active": property_item.is_active,
            }
            for property_item in properties
        ],
        "rules": [
            {
                "name": rule.name,
                "kind": rule.kind,
                "amount": money(rule.amount),
                "cadence": rule.cadence,
                "annual_growth_rate": str(rule.annual_growth_rate) if rule.annual_growth_rate is not None else "",
                "account": rule.account.name if rule.account else "",
                "category": rule.category,
                "is_active": rule.is_active,
            }
            for rule in rules
        ],
        "transfer_rules": [
            {
                "name": rule.name,
                "source_account": rule.source_account.name if rule.source_account else "",
                "target_account": rule.target_account.name,
                "target_account_type": rule.target_account.account_type,
                "amount": money(rule.amount),
                "cadence": rule.cadence,
                "category": rule.category,
                "is_active": rule.is_active,
            }
            for rule in transfer_rules
        ],
        "family_gift_plans": [
            {
                "key": comparable_key("family_gift_plan", gift),
                "name": gift.name,
                "giver": gift.giver.name,
                "recipient": gift.recipient.name,
                "source_account": gift.source_account.name if gift.source_account else "",
                "target_account": gift.target_account.name,
                "amount": money(gift.amount),
                "gift_month": date_value(gift.gift_month),
                "allowance_amount": money(gift.allowance_amount),
                "allowance_window_years": gift.allowance_window_years,
                "purpose": gift.purpose,
                "is_active": gift.is_active,
            }
            for gift in family_gifts
        ],
        "planned_investment_purchases": [
            {
                "key": comparable_key("planned_investment_purchase", purchase),
                "name": purchase.name,
                "asset_type": purchase.asset_type,
                "isin": purchase.isin,
                "ticker": purchase.ticker,
                "source_account": purchase.source_account.name if purchase.source_account else "",
                "target_account": purchase.target_account.name,
                "purchase_amount": money(purchase.purchase_amount),
                "purchase_month": date_value(purchase.purchase_month),
                "payout_date": date_value(purchase.payout_date),
                "payout_amount": money(purchase.payout_amount) if purchase.payout_amount is not None else "",
                "is_active": purchase.is_active,
            }
            for purchase in planned_purchases
        ],
        "real_estate_transfer_plans": [
            {
                "key": comparable_key("real_estate_transfer_plan", transfer),
                "name": transfer.name,
                "property": transfer.property_item.name,
                "giver": transfer.giver.name,
                "recipient": transfer.recipient.name,
                "transfer_month": date_value(transfer.transfer_month),
                "ownership_percent": str(transfer.ownership_percent),
                "taxable_gift_value": money(transfer.taxable_gift_value),
                "allowance_amount": money(transfer.allowance_amount),
                "allowance_window_years": transfer.allowance_window_years,
                "retained_niessbrauch": transfer.retained_niessbrauch,
                "niessbrauch_annual_value": money(transfer.niessbrauch_annual_value),
                "is_active": transfer.is_active,
            }
            for transfer in property_transfers
        ],
        "cash_goals": [
            {
                "name": goal.name,
                "annual_amount": money(goal.annual_amount),
                "start_year": goal.start_year,
                "end_year": goal.end_year,
                "is_active": goal.is_active,
            }
            for goal in cash_goals
        ],
        "projection": {
            "monthly": [projection_month_summary(item) for item in projection],
            "yearly": [projection_year_summary(item) for item in yearly_projection],
        },
    }
