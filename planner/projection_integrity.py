from decimal import Decimal


TOLERANCE = Decimal("0.02")


def _sum_lines(lines, field):
    return sum((getattr(line, field) for line in lines), Decimal("0.00"))


def _close(left, right, tolerance=TOLERANCE):
    return abs(left - right) <= tolerance


def _failure(scope, label, check, expected, actual):
    return {
        "scope": scope,
        "label": label,
        "check": check,
        "expected": expected,
        "actual": actual,
        "difference": actual - expected,
    }


def _check_value(failures, scope, label, check, expected, actual):
    if not _close(expected, actual):
        failures.append(_failure(scope, label, check, expected, actual))
    return 1


def _month_label(month):
    return month.month.strftime("%b %Y")


def _year_label(year):
    return year.label


def check_projection_integrity(projection, yearly_projection=None, accounts=None):
    accounts = list(accounts or [])
    liquid_accounts = [account for account in accounts if account.is_liquid]
    account_by_type = {
        "liquid": [account.id for account in liquid_accounts],
        "depot": [account.id for account in accounts if account.is_invested],
        "loan": [account.id for account in accounts if account.account_type == "loan"],
    }
    failures = []
    check_count = 0
    previous = None
    if projection:
        opening_liquid_accounts = sum((account.effective_balance for account in liquid_accounts), Decimal("0.00"))
        general_pool_balance = projection[0].opening_liquid_balance - opening_liquid_accounts
    else:
        general_pool_balance = Decimal("0.00")

    for month in projection:
        label = _month_label(month)
        cash_effect = _sum_lines(month.audit_lines, "cash_effect")
        invested_effect = _sum_lines(month.audit_lines, "invested_effect")
        other_asset_effect = _sum_lines(month.audit_lines, "other_asset_effect")
        liability_effect = _sum_lines(month.audit_lines, "liability_effect")
        expected_net = (
            month.income
            + month.private_loan_principal
            + month.depot_payout
            + month.depot_draw
            + month.real_estate_sale_proceeds
            - month.expenses
            - month.transfers
        )
        expected_net_worth = (
            month.liquid_balance
            + month.invested_balance
            + month.other_asset_balance
            - month.liability_balance
        )
        check_count += _check_value(
            failures,
            "Month",
            label,
            "Liquid bridge matches audit cash effects",
            month.opening_liquid_balance + cash_effect,
            month.liquid_balance,
        )
        check_count += _check_value(
            failures,
            "Month",
            label,
            "Depot bridge matches audit depot effects",
            month.opening_invested_balance + invested_effect,
            month.invested_balance,
        )
        check_count += _check_value(
            failures,
            "Month",
            label,
            "Other asset bridge matches audit effects",
            month.opening_other_asset_balance + other_asset_effect,
            month.other_asset_balance,
        )
        check_count += _check_value(
            failures,
            "Month",
            label,
            "Liability bridge matches audit effects",
            month.opening_liability_balance + liability_effect,
            month.liability_balance,
        )
        check_count += _check_value(failures, "Month", label, "Net cash formula reconciles", expected_net, month.net)
        check_count += _check_value(
            failures,
            "Month",
            label,
            "Net worth equals assets minus liabilities",
            expected_net_worth,
            month.net_worth,
        )
        check_count += _check_value(
            failures,
            "Month",
            label,
            "Net worth bridge matches audit effects",
            month.opening_net_worth + cash_effect + invested_effect + other_asset_effect - liability_effect,
            month.net_worth,
        )

        if previous:
            check_count += _check_value(
                failures,
                "Month",
                label,
                "Opening liquid equals prior ending liquid",
                previous.liquid_balance,
                month.opening_liquid_balance,
            )
            check_count += _check_value(
                failures,
                "Month",
                label,
                "Opening net worth equals prior ending net worth",
                previous.net_worth,
                month.opening_net_worth,
            )
        previous = month

        liquid_ids = account_by_type["liquid"]
        if liquid_ids:
            account_liquid = sum((month.account_balances.get(account_id, Decimal("0.00")) for account_id in liquid_ids), Decimal("0.00"))
            for line in month.audit_lines:
                liquid_account_effect = sum(
                    (
                        effect["amount"]
                        for effect in line.account_effects
                        if effect.get("account_type") in {"cash", "savings"}
                    ),
                    Decimal("0.00"),
                )
                general_pool_balance += line.cash_effect - liquid_account_effect
            check_count += _check_value(
                failures,
                "Month",
                label,
                "Liquid accounts plus general pool sum to liquid bucket",
                account_liquid + general_pool_balance,
                month.liquid_balance,
            )
        depot_ids = account_by_type["depot"]
        if depot_ids:
            account_depot = sum((month.account_balances.get(account_id, Decimal("0.00")) for account_id in depot_ids), Decimal("0.00"))
            check_count += _check_value(
                failures,
                "Month",
                label,
                "Depot account balances sum to depot bucket",
                account_depot,
                month.invested_balance,
            )
        loan_ids = account_by_type["loan"]
        if loan_ids:
            account_liability = sum((month.account_balances.get(account_id, Decimal("0.00")) for account_id in loan_ids), Decimal("0.00"))
            check_count += _check_value(
                failures,
                "Month",
                label,
                "Loan account balances sum to liability bucket",
                account_liability,
                month.liability_balance,
            )

    for year in yearly_projection or []:
        label = _year_label(year)
        cash_effect = _sum_lines(year.audit_lines, "cash_effect")
        invested_effect = _sum_lines(year.audit_lines, "invested_effect")
        other_asset_effect = _sum_lines(year.audit_lines, "other_asset_effect")
        liability_effect = _sum_lines(year.audit_lines, "liability_effect")
        expected_net = (
            year.income
            + year.private_loan_principal
            + year.depot_payout
            + year.depot_draw
            + year.real_estate_sale_proceeds
            - year.expenses
            - year.transfers
        )
        expected_net_worth = (
            year.ending_liquid_balance
            + year.ending_invested_balance
            + year.ending_other_asset_balance
            - year.ending_liability_balance
        )
        expected_cash_goal_gap = max(year.annual_cash_goal - year.income, Decimal("0.00"))
        check_count += _check_value(
            failures,
            "Year",
            label,
            "Liquid bridge matches audit cash effects",
            year.opening_liquid_balance + cash_effect,
            year.ending_liquid_balance,
        )
        check_count += _check_value(
            failures,
            "Year",
            label,
            "Depot bridge matches audit depot effects",
            year.opening_invested_balance + invested_effect,
            year.ending_invested_balance,
        )
        check_count += _check_value(
            failures,
            "Year",
            label,
            "Other asset bridge matches audit effects",
            year.opening_other_asset_balance + other_asset_effect,
            year.ending_other_asset_balance,
        )
        check_count += _check_value(
            failures,
            "Year",
            label,
            "Liability bridge matches audit effects",
            year.opening_liability_balance + liability_effect,
            year.ending_liability_balance,
        )
        check_count += _check_value(failures, "Year", label, "Net cash formula reconciles", expected_net, year.net)
        check_count += _check_value(
            failures,
            "Year",
            label,
            "Net worth equals assets minus liabilities",
            expected_net_worth,
            year.ending_net_worth,
        )
        check_count += _check_value(
            failures,
            "Year",
            label,
            "Cash goal gap matches goal minus income",
            expected_cash_goal_gap,
            year.cash_goal_gap,
        )

    return {
        "ok": not failures,
        "checked": check_count,
        "failures": failures,
        "failure_count": len(failures),
    }
