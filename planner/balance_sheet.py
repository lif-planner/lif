"""Single source of truth for the household's as-of-now balance sheet.

This logic used to be copy-pasted in three places (the dashboard/Accounts totals
in ``views.account_totals``, the snapshot totals in
``snapshots.build_snapshot_summary``, and the opening month of
``projections.build_projection``). They drifted: the private-loan disbursement
gate was added to two of them but forgotten in the third, double-counting a
future loan in net worth. Keep the rules here so there is one place to change
what counts as a current asset/liability, and a consistency test
(``test_balance_sheet_paths_agree``) holds the projection's opening month to it.
"""

from decimal import Decimal

from .models import AssetAccount


def active_receivables_total(household, as_of_month):
    """Private-loan receivables actually lent out by ``as_of_month``. A loan with a
    future disbursement is still cash in its source account, not yet a receivable —
    counting both would double net worth (finding #22)."""
    return sum(
        (
            loan.current_principal
            for loan in household.private_loans.filter(is_active=True, is_gift=False)
            if loan.disbursed_before(as_of_month)
        ),
        Decimal("0.00"),
    )


def active_real_estate_total(household, as_of_month):
    return sum(
        (
            property_item.current_value
            for property_item in household.properties.filter(is_active=True)
            if property_item.acquired_before(as_of_month)
        ),
        Decimal("0.00"),
    )


def current_balance_sheet(household, accounts=None, as_of_month=None):
    """The household's current balance-sheet totals: liquid, invested, other
    assets, liabilities, and net worth. ``as_of_month`` (default the household
    start month) gates not-yet-disbursed receivables."""
    if accounts is None:
        accounts = list(household.accounts.prefetch_related("holdings"))
    if as_of_month is None:
        as_of_month = household.start_month

    planning_accounts = [account for account in accounts if account.counts_in_household_net_worth]

    liquid = sum((a.effective_balance for a in planning_accounts if a.is_liquid), Decimal("0.00"))
    invested = sum((a.effective_balance for a in planning_accounts if a.is_invested), Decimal("0.00"))
    other = sum((a.effective_balance for a in planning_accounts if a.is_other_asset), Decimal("0.00"))
    other += active_receivables_total(household, as_of_month)
    other += active_real_estate_total(household, as_of_month)
    liability = sum(
        (a.effective_balance for a in planning_accounts if a.account_type == AssetAccount.AccountType.LOAN),
        Decimal("0.00"),
    )

    return {
        "liquid_total": liquid,
        "invested_total": invested,
        "other_asset_total": other,
        "liability_total": liability,
        "net_worth": liquid + invested + other - liability,
    }
