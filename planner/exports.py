import csv
from decimal import Decimal

from django.http import HttpResponse

from .forecast_explain import account_ledger_rows, cash_flow_ledger_rows, general_pool_ledger_rows


PROJECTION_FIELDS = [
    "income",
    "investment_income",
    "depot_growth",
    "depot_payout",
    "depot_draw",
    "depot_income",
    "savings_interest_income",
    "retirement_income",
    "equity_income",
    "private_loan_principal",
    "real_estate_appreciation",
    "real_estate_costs",
    "real_estate_sale_proceeds",
    "rental_income",
    "salary_change_income",
    "child_income",
    "expenses",
    "true_expenses",
    "child_expenses",
    "scenario_income",
    "income_rule_income",
    "scenario_expenses",
    "transfers",
    "debt_interest",
    "debt_principal",
    "net",
]


def csv_response(filename, headers, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_value(value) for value in row])
    return response


def _csv_value(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None:
        return ""
    return value


def projection_month_headers():
    return [
        "month",
        *PROJECTION_FIELDS,
        "opening_liquid_balance",
        "liquid_balance",
        "invested_balance",
        "other_asset_balance",
        "liability_balance",
        "net_worth",
    ]


def projection_month_rows(projection):
    for month in projection:
        yield [
            month.month.strftime("%Y-%m"),
            *[getattr(month, field) for field in PROJECTION_FIELDS],
            month.opening_liquid_balance,
            month.liquid_balance,
            month.invested_balance,
            month.other_asset_balance,
            month.liability_balance,
            month.net_worth,
        ]


def projection_year_headers():
    return [
        "year",
        "label",
        "month_count",
        *PROJECTION_FIELDS,
        "annual_cash_goal",
        "cash_goal_coverage_percent",
        "cash_goal_gap",
        "portfolio_draw_percent",
        "ending_liquid_balance",
        "ending_invested_balance",
        "ending_other_asset_balance",
        "ending_liability_balance",
        "ending_net_worth",
        "lowest_liquid_balance",
        "stress_months",
    ]


def projection_year_rows(yearly_projection):
    for year in yearly_projection:
        yield [
            year.year,
            year.label,
            year.month_count,
            *[getattr(year, field) for field in PROJECTION_FIELDS],
            year.annual_cash_goal,
            year.cash_goal_coverage_percent,
            year.cash_goal_gap,
            year.portfolio_draw_percent,
            year.ending_liquid_balance,
            year.ending_invested_balance,
            year.ending_other_asset_balance,
            year.ending_liability_balance,
            year.ending_net_worth,
            year.lowest_liquid_balance,
            year.stress_months,
        ]


def cash_flow_headers():
    return ["month", "year", "section", "group", "name", "account", "amount", "note", "detail_index"]


def cash_flow_rows(projection, accounts_by_id):
    for row in cash_flow_ledger_rows(projection):
        account = accounts_by_id.get(row["account_id"])
        yield [
            row["month"].strftime("%Y-%m"),
            row["year"],
            row["section"],
            row["group_title"],
            row["name"],
            account.name if account else "General liquid pool",
            row["amount"],
            row["note"],
            row["index"],
        ]


def statement_headers(account_label="account"):
    return ["month", "year", account_label, "section", "name", "amount", "ending_balance", "note", "detail_index"]


def account_statement_rows(projection, account):
    for row in account_ledger_rows(projection, account):
        yield [
            row["month"].strftime("%Y-%m"),
            row["month"].year,
            account.name,
            row["section"],
            row["name"],
            row["amount"],
            row["ending_balance"],
            row["note"],
            row["index"],
        ]


def general_pool_statement_rows(projection, household):
    for row in general_pool_ledger_rows(projection, household):
        yield [
            row["month"].strftime("%Y-%m"),
            row["month"].year,
            "General liquid pool",
            row["section"],
            row["name"],
            row["amount"],
            row["ending_balance"],
            row["note"],
            row["index"],
        ]
