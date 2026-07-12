from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import groupby

from .finance import monthly_rate_from_annual_percent, quantize_money
from .models import AssetAccount, DepotHolding, FamilyGiftPlan, MoneyRule, PlannedInvestmentPurchase, RealEstate, RealEstateTransferPlan, TransferRule, TrueExpense


@dataclass(frozen=True)
class ProjectionLine:
    section: str
    name: str
    amount: Decimal
    cash_effect: Decimal
    invested_effect: Decimal
    other_asset_effect: Decimal
    liability_effect: Decimal
    note: str = ""
    account_effects: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class ProjectionMonth:
    index: int
    month: date
    opening_liquid_balance: Decimal
    opening_invested_balance: Decimal
    opening_other_asset_balance: Decimal
    opening_liability_balance: Decimal
    opening_net_worth: Decimal
    income: Decimal
    investment_income: Decimal
    depot_growth: Decimal
    depot_payout: Decimal
    depot_draw: Decimal
    depot_income: Decimal
    savings_interest_income: Decimal
    retirement_income: Decimal
    equity_income: Decimal
    private_loan_principal: Decimal
    real_estate_appreciation: Decimal
    real_estate_costs: Decimal
    real_estate_sale_proceeds: Decimal
    rental_income: Decimal
    salary_change_income: Decimal
    child_income: Decimal
    expenses: Decimal
    true_expenses: Decimal
    child_expenses: Decimal
    scenario_income: Decimal
    income_rule_income: Decimal
    scenario_expenses: Decimal
    transfers: Decimal
    debt_interest: Decimal
    debt_principal: Decimal
    net: Decimal
    liquid_balance: Decimal
    invested_balance: Decimal
    other_asset_balance: Decimal
    liability_balance: Decimal
    balance: Decimal
    net_worth: Decimal
    account_balances: dict
    audit_lines: list


@dataclass(frozen=True)
class ProjectionYear:
    year: int
    label: str
    start_index: int
    end_index: int
    month_count: int
    opening_liquid_balance: Decimal
    opening_invested_balance: Decimal
    opening_other_asset_balance: Decimal
    opening_liability_balance: Decimal
    opening_net_worth: Decimal
    income: Decimal
    investment_income: Decimal
    depot_growth: Decimal
    depot_payout: Decimal
    depot_draw: Decimal
    depot_income: Decimal
    savings_interest_income: Decimal
    retirement_income: Decimal
    equity_income: Decimal
    private_loan_principal: Decimal
    real_estate_appreciation: Decimal
    real_estate_costs: Decimal
    real_estate_sale_proceeds: Decimal
    rental_income: Decimal
    salary_change_income: Decimal
    child_income: Decimal
    expenses: Decimal
    true_expenses: Decimal
    child_expenses: Decimal
    scenario_income: Decimal
    income_rule_income: Decimal
    scenario_expenses: Decimal
    transfers: Decimal
    debt_interest: Decimal
    debt_principal: Decimal
    net: Decimal
    annual_cash_goal: Decimal
    cash_goal_coverage_percent: Decimal
    cash_goal_gap: Decimal
    portfolio_draw_percent: Decimal
    ending_liquid_balance: Decimal
    ending_invested_balance: Decimal
    ending_other_asset_balance: Decimal
    ending_liability_balance: Decimal
    ending_net_worth: Decimal
    lowest_liquid_balance: Decimal
    stress_months: int
    audit_lines: list


def add_months(value, months):
    year = value.year + ((value.month - 1 + months) // 12)
    month = ((value.month - 1 + months) % 12) + 1
    return date(year, month, 1)


def first_of_month(value):
    return date(value.year, value.month, 1)


def months_between(start, end):
    return (end.year - start.year) * 12 + (end.month - start.month)


def rule_applies(rule, month, projection_start=None):
    if not rule.is_active:
        return False
    rule_start = first_of_month(rule.start_month) if rule.start_month else None
    rule_end = first_of_month(rule.end_month) if rule.end_month else None
    if rule_start and month < rule_start:
        return False
    if rule_end and month > rule_end:
        return False
    if rule.cadence == TransferRule.Cadence.ONCE:
        anchor = rule_start or projection_start
        if anchor is None:
            return False
        return month == anchor
    if rule.cadence == MoneyRule.Cadence.YEARLY:
        # Anchor the once-a-year hit to the rule's own start, or the projection
        # start when no start month is set. Falling back to `month` would make
        # the comparison always true and apply the full amount every month.
        anchor = rule_start or projection_start
        if anchor is None:
            return False
        return month.month == anchor.month
    return True


def item_applies(item, month):
    if not item.is_active:
        return False
    item_start = first_of_month(item.start_month) if item.start_month else None
    item_end = first_of_month(item.end_month) if item.end_month else None
    if item_start and month < item_start:
        return False
    if item_end and month > item_end:
        return False
    return True


def cents(value):
    return quantize_money(value)


def percent(value):
    return quantize_money(value)


def cash_goal_for_year(year, cash_goals):
    matching = [
        goal
        for goal in cash_goals
        if goal.is_active and goal.start_year <= year and not (goal.end_year and goal.end_year < year)
    ]
    if not matching:
        return None
    # When active goals overlap a year, the most recently effective one wins
    # (latest start year), with a deterministic tie-break so the result never
    # depends on query ordering.
    return max(matching, key=lambda goal: (goal.start_year, goal.end_year or 0, goal.name))


def cash_goal_amount_for_year(goal, year, annual_inflation_rate=None, multiplier=Decimal("1.00")):
    if not goal:
        return Decimal("0.00")
    multiplier = multiplier if multiplier is not None else Decimal("1.00")
    annual_amount = goal.annual_amount * multiplier
    if not goal.indexed_to_inflation:
        return cents(annual_amount)
    years_after_start = max(year - goal.start_year, 0)
    rate = annual_inflation_rate if annual_inflation_rate is not None else goal.household.annual_inflation_rate
    inflation_rate = rate / Decimal("100")
    return cents(annual_amount * ((Decimal("1.00") + inflation_rate) ** years_after_start))


def savings_interest_applies(account, index):
    if account.account_type != AssetAccount.AccountType.SAVINGS:
        return False
    if account.savings_annual_interest_rate <= 0:
        return False
    cadence_months = {
        AssetAccount.InterestCadence.MONTHLY: 1,
        AssetAccount.InterestCadence.QUARTERLY: 3,
        AssetAccount.InterestCadence.YEARLY: 12,
    }[account.savings_interest_cadence]
    return (index + 1) % cadence_months == 0


def savings_gross_interest(account, balance):
    cadence_months = {
        AssetAccount.InterestCadence.MONTHLY: 1,
        AssetAccount.InterestCadence.QUARTERLY: 3,
        AssetAccount.InterestCadence.YEARLY: 12,
    }[account.savings_interest_cadence]
    return cents(balance * (account.savings_annual_interest_rate / Decimal("100")) * (Decimal(cadence_months) / Decimal("12")))


DISTRIBUTION_CADENCE_MONTHS = {
    AssetAccount.InterestCadence.MONTHLY: 1,
    AssetAccount.InterestCadence.QUARTERLY: 3,
    AssetAccount.InterestCadence.YEARLY: 12,
}


def depot_distribution_applies(account, index):
    # Holdings-valued depots model distributions per holding instead (see
    # holding_distribution_applies), since not every holding in a depot pays out.
    if account.account_type != AssetAccount.AccountType.DEPOT:
        return False
    if account.uses_holdings_valuation:
        return False
    if account.depot_annual_distribution_rate <= 0:
        return False
    cadence_months = DISTRIBUTION_CADENCE_MONTHS[account.depot_distribution_cadence]
    return (index + 1) % cadence_months == 0


def depot_gross_distribution(account, balance):
    cadence_months = DISTRIBUTION_CADENCE_MONTHS[account.depot_distribution_cadence]
    return cents(balance * (account.depot_annual_distribution_rate / Decimal("100")) * (Decimal(cadence_months) / Decimal("12")))


# These two also accept a PlannedInvestmentPurchase: it carries the same
# annual_distribution_rate/distribution_cadence fields as DepotHolding.
def holding_distribution_applies(holding, index):
    if holding.annual_distribution_rate <= 0:
        return False
    cadence_months = DISTRIBUTION_CADENCE_MONTHS[holding.distribution_cadence]
    return (index + 1) % cadence_months == 0


def holding_gross_distribution(holding, value):
    cadence_months = DISTRIBUTION_CADENCE_MONTHS[holding.distribution_cadence]
    return cents(value * (holding.annual_distribution_rate / Decimal("100")) * (Decimal(cadence_months) / Decimal("12")))


def debt_terms_for_month(debt, month):
    refinance_from = first_of_month(debt.effective_refinance_from_month) if debt.effective_refinance_from_month else None
    if (
        refinance_from
        and month >= refinance_from
        and debt.refinance_annual_interest_rate is not None
        and debt.refinance_monthly_payment is not None
    ):
        # Loans use the nominal monthly rate (APR / 12), matching how banks quote
        # a Sollzins, rather than the compounded conversion used for depot growth.
        monthly_rate = debt.refinance_annual_interest_rate / Decimal("100") / Decimal("12")
        return monthly_rate, debt.refinance_monthly_payment
    return debt.monthly_interest_rate, debt.monthly_payment


def amortize_month(principal, monthly_rate, monthly_payment, interest_only=False):
    """Apply one month of interest and payment to a loan balance.

    Returns (interest, payment, principal_portion, new_principal). The payment is
    capped so the final instalment never overshoots the balance; if it cannot
    cover the interest the balance grows (negative amortization). During an
    interest-only window the payment services exactly the interest and the
    principal does not move.
    """
    interest = cents(principal * monthly_rate)
    if interest_only:
        return interest, interest, Decimal("0.00"), principal
    payment = min(monthly_payment, principal + interest)
    principal_portion = max(payment - interest, Decimal("0.00"))
    new_principal = principal + interest - payment
    return interest, payment, principal_portion, new_principal


def debt_month_plan(debt, month):
    """Return (monthly_rate, monthly_payment, interest_only) for a given month."""
    monthly_rate, monthly_payment = debt_terms_for_month(debt, month)
    interest_only = bool(debt.interest_only_until) and month < first_of_month(debt.interest_only_until)
    return monthly_rate, monthly_payment, interest_only


def debt_extra_payment_for_month(debt, month, remaining_principal):
    """Yearly Sondertilgung applied on the anchor calendar month, capped at the
    balance still owed after the scheduled payment."""
    extra = debt.annual_extra_payment or Decimal("0.00")
    if extra <= 0 or remaining_principal <= 0:
        return Decimal("0.00")
    anchor_month = debt.extra_payment_month or (debt.start_month.month if debt.start_month else month.month)
    if month.month != anchor_month:
        return Decimal("0.00")
    return min(extra, remaining_principal)


def effective_income_growth_rate(item, default_growth_rate):
    if item.annual_growth_rate is None:
        return default_growth_rate
    return item.annual_growth_rate


def growth_adjusted_amount(amount, annual_growth_rate, anchor_month, month):
    if not annual_growth_rate:
        return amount
    years_after_anchor = max(months_between(first_of_month(anchor_month), month) // 12, 0)
    if years_after_anchor <= 0:
        return amount
    growth_factor = (Decimal("1.00") + annual_growth_rate / Decimal("100")) ** years_after_anchor
    return cents(amount * growth_factor)


# Upper bound when projecting a single loan to its payoff, independent of the
# household planning horizon (100 years of months).
MAX_AMORTIZATION_MONTHS = 1200


def iter_debt_schedule(debt, projection_start, max_months=MAX_AMORTIZATION_MONTHS):
    """Yield one entry per applied month for a single debt: the canonical
    per-month amortization (interest, scheduled principal, yearly extra payment,
    total payment, ending balance). Stops at payoff or when the debt no longer
    applies. Shared by the projection summary and the schedule page so the two
    can never drift."""
    principal = debt.current_principal or Decimal("0.00")
    for index in range(max_months):
        if principal <= 0:
            break
        month = add_months(projection_start, index)
        if not item_applies(debt, month):
            continue
        monthly_rate, monthly_payment, interest_only = debt_month_plan(debt, month)
        interest, scheduled_payment, scheduled_principal, principal = amortize_month(
            principal, monthly_rate, monthly_payment, interest_only
        )
        extra = debt_extra_payment_for_month(debt, month, principal)
        principal -= extra
        yield {
            "index": index,
            "month": month,
            "interest": interest,
            "principal": scheduled_principal,
            "extra": extra,
            "payment": scheduled_payment + extra,
            "interest_only": interest_only,
            "ending_principal": cents(max(principal, Decimal("0.00"))),
        }
        if principal <= 0:
            break


def summarize_debt(debt, projection_start, max_months=MAX_AMORTIZATION_MONTHS):
    """Simulate a single debt to estimate payoff date and lifetime interest.

    ``ending_principal`` is the balance still owed once the debt stops applying,
    which is non-zero when a debt's ``end_month`` arrives before payoff.
    """
    total_interest = Decimal("0.00")
    total_principal_paid = Decimal("0.00")
    payoff_index = None
    ending_principal = cents(max(debt.current_principal or Decimal("0.00"), Decimal("0.00")))
    for entry in iter_debt_schedule(debt, projection_start, max_months):
        total_interest += entry["interest"]
        total_principal_paid += entry["principal"] + entry["extra"]
        ending_principal = entry["ending_principal"]
        if entry["ending_principal"] <= 0:
            payoff_index = entry["index"]
    return {
        "payoff_index": payoff_index,
        "payoff_month": add_months(projection_start, payoff_index) if payoff_index is not None else None,
        "months_to_payoff": (payoff_index + 1) if payoff_index is not None else None,
        "total_interest": cents(total_interest),
        "total_principal_paid": cents(total_principal_paid),
        "ending_principal": ending_principal,
    }


def retirement_applies(plan, month):
    if not plan.is_active:
        return False
    start = first_of_month(plan.retirement_start_month)
    end = first_of_month(plan.end_month) if plan.end_month else None
    if month < start:
        return False
    if end and month > end:
        return False
    return True


def retirement_monthly_income(plan, projection_start, month, deduction_rate=None):
    months_until_retirement = max(months_between(projection_start, first_of_month(plan.retirement_start_month)), 0)
    years_until_retirement = Decimal(months_until_retirement) / Decimal("12")
    projected_points = plan.current_pension_points + (plan.expected_annual_points * years_until_retirement)
    base_monthly = (projected_points * plan.pension_value_per_point) + plan.private_monthly_pension
    months_after_retirement = max(months_between(first_of_month(plan.retirement_start_month), month), 0)
    adjustment_years = months_after_retirement // 12
    adjustment_factor = (Decimal("1.00") + (plan.annual_adjustment_rate / Decimal("100"))) ** adjustment_years
    gross = cents(base_monthly * adjustment_factor)
    # Net pension income hits the projection: deduct pension tax and health
    # insurance so liquid/net worth are after-tax. The tax-aware analytics read
    # this net value back and do not deduct again.
    if deduction_rate is None:
        deduction_rate = Decimal("0.00")
    if deduction_rate and deduction_rate < Decimal("100"):
        return cents(gross * (Decimal("1.00") - deduction_rate / Decimal("100")))
    return gross


def retirement_contribution_applies(plan, month):
    if not plan.is_active or plan.monthly_contribution <= 0:
        return False
    start = first_of_month(plan.contribution_start_month) if plan.contribution_start_month else None
    end = first_of_month(plan.contribution_end_month) if plan.contribution_end_month else None
    retirement_start = first_of_month(plan.retirement_start_month)
    if start and month < start:
        return False
    if end:
        if month > end:
            return False
    elif month >= retirement_start:
        return False
    return True


def equity_grant_applies(grant, month):
    if not grant.is_active:
        return False
    first = first_of_month(grant.first_vest_month)
    last = first_of_month(grant.last_vest_month)
    if month < first or month > last:
        return False
    elapsed_months = months_between(first, month)
    cadence_months = {
        "monthly": 1,
        "quarterly": 3,
        "yearly": 12,
    }[grant.cadence]
    return elapsed_months % cadence_months == 0


def true_expense_applies(expense, month):
    if not expense.is_active:
        return False
    first = first_of_month(expense.first_due_month)
    end = first_of_month(expense.end_month) if expense.end_month else None
    if month < first:
        return False
    if end and month > end:
        return False
    elapsed_months = months_between(first, month)
    cadence_months = {
        TrueExpense.Cadence.MONTHLY: 1,
        TrueExpense.Cadence.QUARTERLY: 3,
        TrueExpense.Cadence.YEARLY: 12,
        TrueExpense.Cadence.ONCE: None,
    }[expense.cadence]
    if cadence_months is None:
        return elapsed_months == 0
    return elapsed_months >= 0 and elapsed_months % cadence_months == 0


def projection_line(
    section,
    name,
    amount,
    cash_effect=Decimal("0.00"),
    invested_effect=Decimal("0.00"),
    other_asset_effect=Decimal("0.00"),
    liability_effect=Decimal("0.00"),
    note="",
    account_effects=None,
):
    return ProjectionLine(
        section=section,
        name=name,
        amount=amount,
        cash_effect=cash_effect,
        invested_effect=invested_effect,
        other_asset_effect=other_asset_effect,
        liability_effect=liability_effect,
        note=note,
        account_effects=tuple(effect for effect in (account_effects or ()) if effect),
    )


def account_effect(account, amount):
    if not account:
        return None
    return {
        "account_id": account.id,
        "account_name": account.name,
        "account_type": account.account_type,
        "amount": amount,
    }


def account_counts_in_household(account):
    return not account or account.counts_in_household_net_worth


def credit_liquid_account(context, account, amount):
    if account:
        if account.account_type == AssetAccount.AccountType.CASH:
            context.cash_balances[account.id] = context.cash_balances.get(account.id, account.effective_balance) + amount
        elif account.account_type == AssetAccount.AccountType.SAVINGS:
            context.savings_balances[account.id] = context.savings_balances.get(
                account.id,
                account.effective_balance,
            ) + amount
    if account_counts_in_household(account):
        context.liquid_balance += amount


def debit_liquid_account(context, account, amount):
    if account:
        if account.account_type == AssetAccount.AccountType.CASH:
            context.cash_balances[account.id] = context.cash_balances.get(account.id, account.effective_balance) - amount
        elif account.account_type == AssetAccount.AccountType.SAVINGS:
            context.savings_balances[account.id] = context.savings_balances.get(
                account.id,
                account.effective_balance,
            ) - amount
    if account_counts_in_household(account):
        context.liquid_balance -= amount


def account_balance_snapshot(context, accounts):
    balances = {}
    for account in accounts:
        if account.account_type == AssetAccount.AccountType.CASH:
            balances[account.id] = context.cash_balances.get(account.id, account.effective_balance)
        elif account.account_type == AssetAccount.AccountType.SAVINGS:
            balances[account.id] = context.savings_balances.get(account.id, account.effective_balance)
        elif account.account_type == AssetAccount.AccountType.DEPOT:
            balances[account.id] = context.depot_balances.get(account.id, account.effective_balance)
        elif account.account_type == AssetAccount.AccountType.LOAN:
            linked_debt = context.debt_by_account_id.get(account.id)
            if linked_debt is not None:
                balances[account.id] = context.debt_balances.get(linked_debt.id, linked_debt.current_principal)
            else:
                balances[account.id] = account.effective_balance
        else:
            balances[account.id] = account.effective_balance
    return balances


@dataclass
class ProjectionContext:
    """Mutable running state threaded through the contributors for the whole
    projection: cross-month balances, per-entity ledgers, and shared config."""

    projection_start: date
    liquid_balance: Decimal
    invested_balance: Decimal
    other_asset_balance: Decimal
    liability_balance: Decimal
    debt_balances: dict
    cash_balances: dict
    savings_balances: dict
    depot_balances: dict
    depot_year_opening_balances: dict
    depot_gross_distributions_by_year: dict
    private_loan_balances: dict
    real_estate_balances: dict
    debt_by_account_id: dict
    depot_growth_rates: dict
    depot_monthly_return_rates: list
    default_operating_account: object
    default_income_growth_rate: Decimal
    retirement_deduction_rate: Decimal
    capital_tax_rate: Decimal
    income_tax_rate: Decimal
    capital_income_allowance: Decimal
    capital_allowance_used: dict
    vorabpauschale_basiszins_rate: Decimal


@dataclass
class MonthState:
    """Per-month accumulators a contributor adds to; flushed into a
    ``ProjectionMonth`` at the end of each month."""

    index: int
    month: date
    income: Decimal = Decimal("0.00")
    investment_income: Decimal = Decimal("0.00")
    depot_growth: Decimal = Decimal("0.00")
    depot_payout: Decimal = Decimal("0.00")
    depot_draw: Decimal = Decimal("0.00")
    depot_income: Decimal = Decimal("0.00")
    savings_interest_income: Decimal = Decimal("0.00")
    retirement_income: Decimal = Decimal("0.00")
    equity_income: Decimal = Decimal("0.00")
    private_loan_principal: Decimal = Decimal("0.00")
    real_estate_appreciation: Decimal = Decimal("0.00")
    real_estate_costs: Decimal = Decimal("0.00")
    real_estate_sale_proceeds: Decimal = Decimal("0.00")
    rental_income: Decimal = Decimal("0.00")
    salary_change_income: Decimal = Decimal("0.00")
    child_income: Decimal = Decimal("0.00")
    expenses: Decimal = Decimal("0.00")
    true_expenses: Decimal = Decimal("0.00")
    child_expenses: Decimal = Decimal("0.00")
    scenario_income: Decimal = Decimal("0.00")
    income_rule_income: Decimal = Decimal("0.00")
    scenario_expenses: Decimal = Decimal("0.00")
    transfers: Decimal = Decimal("0.00")
    debt_interest: Decimal = Decimal("0.00")
    debt_principal: Decimal = Decimal("0.00")
    audit_lines: list = field(default_factory=list)


def capital_allowance_remaining(context, year):
    used = context.capital_allowance_used.get(year, Decimal("0.00"))
    return max(context.capital_income_allowance - used, Decimal("0.00"))


def taxable_fraction_after_exemption(exemption_rate):
    return max(Decimal("0.00"), Decimal("1.00") - (exemption_rate / Decimal("100")))


def apply_capital_tax(context, state, gross, tax_rate, partial_exemption_rate=Decimal("0.00")):
    gross = cents(gross)
    if gross <= 0:
        return Decimal("0.00"), Decimal("0.00"), Decimal("0.00")
    year = state.month.year
    taxable_base = cents(gross * taxable_fraction_after_exemption(partial_exemption_rate))
    exempt = min(taxable_base, capital_allowance_remaining(context, year))
    taxable = max(taxable_base - exempt, Decimal("0.00"))
    tax = cents(taxable * tax_rate / Decimal("100"))
    context.capital_allowance_used[year] = context.capital_allowance_used.get(year, Decimal("0.00")) + exempt
    return cents(gross - tax), tax, exempt


def net_capital_payout(context, state, gross_payout, cost_basis, partial_exemption_rate=Decimal("0.00")):
    gross_payout = cents(gross_payout)
    cost_basis = cents(cost_basis)
    gain = cents(max(gross_payout - cost_basis, Decimal("0.00")))
    if gain <= 0:
        return gross_payout, Decimal("0.00"), Decimal("0.00"), gain
    net_gain, tax, allowance_used = apply_capital_tax(
        context,
        state,
        gain,
        context.capital_tax_rate,
        partial_exemption_rate=partial_exemption_rate,
    )
    return cents(cost_basis + net_gain), tax, allowance_used, gain


def gross_for_net_after_capital_tax(context, state, net_needed, tax_rate, partial_exemption_rate=Decimal("0.00")):
    net_needed = cents(net_needed)
    if net_needed <= 0:
        return Decimal("0.00")
    tax_fraction = tax_rate / Decimal("100")
    if tax_fraction >= Decimal("1.00"):
        return net_needed
    remaining_allowance = capital_allowance_remaining(context, state.month.year)
    taxable_fraction = taxable_fraction_after_exemption(partial_exemption_rate)
    if taxable_fraction <= 0:
        return net_needed
    tax_free_gross_threshold = cents(remaining_allowance / taxable_fraction) if remaining_allowance else Decimal("0.00")
    if net_needed <= tax_free_gross_threshold:
        return net_needed
    denominator = Decimal("1.00") - (tax_fraction * taxable_fraction)
    if denominator <= 0:
        return net_needed
    return cents((net_needed - (tax_fraction * remaining_allowance)) / denominator)


# Each contributor models one financial concept and mutates the shared context
# and the month's accumulators. They run in a fixed order; balances flow between
# them within a month (e.g. a debt overpayment rule reads the post-amortization
# principal). Add a new concept by writing a contributor and registering it in
# ``build_projection`` rather than extending the month loop.


class DepotGrowthContributor:
    def __init__(self, accounts):
        self.accounts = accounts

    def apply(self, context, state):
        for account in self.accounts:
            growth_rate = (
                context.depot_monthly_return_rates[state.index]
                if state.index < len(context.depot_monthly_return_rates)
                else context.depot_growth_rates.get(account.id, Decimal("0.00"))
            )
            if not growth_rate:
                continue
            base_value = context.depot_balances.get(account.id, Decimal("0.00"))
            if base_value <= 0:
                continue
            growth = cents(base_value * growth_rate)
            if not growth:
                continue
            context.depot_balances[account.id] = base_value + growth
            if not account.counts_in_household_net_worth:
                continue
            context.invested_balance += growth
            state.depot_growth += growth
            state.audit_lines.append(
                projection_line(
                    "Depot growth",
                    account.name,
                    growth,
                    invested_effect=growth,
                    note=f"{account.depot_annual_return_rate}% annual return assumption",
                )
            )


class DepotVorabpauschaleContributor:
    def __init__(self, accounts):
        self.accounts = [
            account
            for account in accounts
            if account.account_type == AssetAccount.AccountType.DEPOT and account.counts_in_household_net_worth
        ]

    def record_openings(self, context, year):
        for account in self.accounts:
            context.depot_year_opening_balances.setdefault(
                (year, account.id),
                context.depot_balances.get(account.id, account.effective_balance),
            )

    def apply_prior_year_tax(self, context, state):
        if state.month.month != 1:
            return
        prior_year = state.month.year - 1
        for account in self.accounts:
            if not account.depot_vorabpauschale_enabled:
                continue
            opening = context.depot_year_opening_balances.get((prior_year, account.id))
            if opening is None or opening <= 0:
                continue
            current = context.depot_balances.get(account.id, account.effective_balance)
            gross_distributions = context.depot_gross_distributions_by_year.get(
                (prior_year, account.id),
                Decimal("0.00"),
            )
            base_return = cents(opening * (context.vorabpauschale_basiszins_rate / Decimal("100")) * Decimal("0.70"))
            capped_return = min(base_return, max(current - opening + gross_distributions, Decimal("0.00")))
            vorabpauschale = cents(max(capped_return - gross_distributions, Decimal("0.00")))
            if vorabpauschale <= 0:
                continue
            _, tax, allowance_used = apply_capital_tax(
                context,
                state,
                vorabpauschale,
                context.capital_tax_rate,
                partial_exemption_rate=account.depot_teilfreistellung_rate,
            )
            operating = context.default_operating_account
            if tax:
                state.expenses += tax
                debit_liquid_account(context, operating, tax)
            state.audit_lines.append(
                projection_line(
                    "Vorabpauschale",
                    account.name,
                    tax,
                    cash_effect=-tax,
                    note=(
                        f"{prior_year} notional base {vorabpauschale}, "
                        f"{account.depot_teilfreistellung_rate}% partial exemption, "
                        f"{allowance_used} allowance, {tax} tax at {context.capital_tax_rate}%"
                    ),
                    account_effects=[account_effect(operating, -tax)],
                )
            )

    def apply(self, context, state):
        self.apply_prior_year_tax(context, state)
        self.record_openings(context, state.month.year)


class DepotPayoutContributor:
    def __init__(self, holdings):
        self.holdings = holdings

    def apply(self, context, state):
        for holding in self.holdings:
            if not holding.payout_date or first_of_month(holding.payout_date) != state.month:
                continue
            payout = cents(holding.expected_payout_amount)
            invested_release = cents(holding.current_value)
            if payout <= 0:
                continue
            depot_balance = context.depot_balances.get(
                holding.asset_account_id,
                holding.asset_account.effective_balance,
            )
            invested_release = min(invested_release, max(depot_balance, Decimal("0.00")))
            if not invested_release and not payout:
                continue
            net_payout, tax, allowance_used, taxable_gain = net_capital_payout(
                context,
                state,
                payout,
                invested_release,
                partial_exemption_rate=holding.asset_account.depot_teilfreistellung_rate,
            )
            operating = context.default_operating_account
            context.depot_balances[holding.asset_account_id] = depot_balance - invested_release
            context.invested_balance -= invested_release
            credit_liquid_account(context, operating, net_payout)
            state.depot_payout += net_payout
            gain_or_loss = cents(payout - invested_release)
            note = f"Payout/maturity date {holding.payout_date:%Y-%m-%d}"
            if gain_or_loss:
                note += f", expected return since valuation {gain_or_loss}"
            if tax:
                note += (
                    f", {taxable_gain} taxable gain, {holding.asset_account.depot_teilfreistellung_rate}% "
                    f"partial exemption, {allowance_used} allowance, {tax} capital tax"
                )
            state.audit_lines.append(
                projection_line(
                    "Depot payout",
                    holding.name,
                    net_payout,
                    cash_effect=net_payout,
                    invested_effect=-invested_release,
                    note=note,
                    account_effects=[
                        account_effect(holding.asset_account, -invested_release),
                        account_effect(operating, net_payout),
                    ],
                )
            )


class RealEstateContributor:
    def __init__(self, properties, transfer_plans=None):
        self.properties = properties
        self.transfer_plans_by_property = {}
        for plan in transfer_plans or []:
            self.transfer_plans_by_property.setdefault(plan.property_item_id, []).append(plan)
        self.retained_niessbrauch_property_ids = set()

    def funding_account(self, context, property_item):
        return property_item.source_account or context.default_operating_account

    def sale_account(self, context, property_item):
        return property_item.sale_proceeds_account or context.default_operating_account

    def owned_value(self, context, property_item):
        return context.real_estate_balances.get(property_item.id, Decimal("0.00"))

    def has_living_benefit(self, property_item):
        return property_item.id in self.retained_niessbrauch_property_ids

    def apply_transfer_plans(self, context, state, property_item):
        for plan in self.transfer_plans_by_property.get(property_item.id, []):
            if not plan.is_active or first_of_month(plan.transfer_month) != state.month:
                continue
            owned_value = self.owned_value(context, property_item)
            if owned_value <= 0:
                continue
            transfer_value = cents(owned_value * plan.ownership_percent / Decimal("100"))
            if transfer_value <= 0:
                continue
            context.real_estate_balances[property_item.id] = max(owned_value - transfer_value, Decimal("0.00"))
            context.other_asset_balance -= transfer_value
            if plan.retained_niessbrauch:
                self.retained_niessbrauch_property_ids.add(property_item.id)
            note = (
                f"{plan.ownership_percent}% transferred from {plan.giver.name} to {plan.recipient.name}; "
                f"taxable gift value {plan.taxable_gift_value}, allowance {plan.allowance_amount} "
                f"for {plan.window_start_year}-{plan.window_end_year}"
            )
            if plan.retained_niessbrauch:
                note += ", Nießbrauch retained"
            state.audit_lines.append(
                projection_line(
                    "Property transfer",
                    plan.name,
                    transfer_value,
                    other_asset_effect=-transfer_value,
                    note=note,
                )
            )

    def apply(self, context, state):
        for property_item in self.properties:
            if not property_item.is_active:
                continue
            acquired = property_item.id in context.real_estate_balances
            if (
                property_item.acquisition_month
                and not property_item.acquired_before(context.projection_start)
                and state.month == first_of_month(property_item.acquisition_month)
                and not acquired
            ):
                account = self.funding_account(context, property_item)
                cash_out = property_item.down_payment + property_item.acquisition_costs
                credit_liquid_account(context, account, -cash_out)
                context.real_estate_balances[property_item.id] = property_item.current_value
                context.other_asset_balance += property_item.current_value
                opening_debt_total = Decimal("0.00")
                for debt in property_item.debts.all():
                    context.debt_balances[debt.id] = debt.current_principal
                    opening_debt_total += debt.current_principal
                context.liability_balance += opening_debt_total
                state.transfers += property_item.down_payment
                state.expenses += property_item.acquisition_costs
                state.real_estate_costs += property_item.acquisition_costs
                note = f"{property_item.down_payment} down payment, {property_item.acquisition_costs} acquisition costs"
                if account:
                    note += f", from {account.name}"
                state.audit_lines.append(
                    projection_line(
                        "Property purchase",
                        property_item.name,
                        cash_out,
                        cash_effect=-cash_out,
                        other_asset_effect=property_item.current_value,
                        liability_effect=opening_debt_total,
                        note=note,
                        account_effects=[account_effect(account, -cash_out)],
                    )
                )
                acquired = True

            if not acquired:
                continue

            self.apply_transfer_plans(context, state, property_item)
            owned_value = self.owned_value(context, property_item)
            retained_niessbrauch = self.has_living_benefit(property_item)
            if owned_value <= 0 and not retained_niessbrauch:
                context.real_estate_balances.pop(property_item.id, None)
                continue

            if property_item.sale_month and state.month == first_of_month(property_item.sale_month):
                sale_price = self.owned_value(context, property_item)
                if sale_price <= 0:
                    continue
                sale_costs = cents(sale_price * property_item.sale_costs_rate / Decimal("100"))
                gain = max(sale_price - property_item.current_value, Decimal("0.00"))
                capital_tax = cents(gain * property_item.capital_gains_tax_rate / Decimal("100"))
                payoff = Decimal("0.00")
                for debt in property_item.debts.all():
                    debt_payoff = context.debt_balances.get(debt.id, Decimal("0.00"))
                    context.debt_balances[debt.id] = Decimal("0.00")
                    payoff += debt_payoff
                context.liability_balance -= payoff
                gross_cash_after_payoff = sale_price - payoff
                net = gross_cash_after_payoff - sale_costs - capital_tax
                account = self.sale_account(context, property_item)
                credit_liquid_account(context, account, net)
                context.other_asset_balance -= sale_price
                context.real_estate_balances.pop(property_item.id, None)
                state.real_estate_sale_proceeds += gross_cash_after_payoff
                state.expenses += sale_costs + capital_tax
                state.real_estate_costs += sale_costs + capital_tax
                note = f"{sale_costs} sale costs, {payoff} mortgage payoff, {capital_tax} capital gains tax"
                if account:
                    note += f", to {account.name}"
                state.audit_lines.append(
                    projection_line(
                        "Property sale",
                        property_item.name,
                        net,
                        cash_effect=net,
                        other_asset_effect=-sale_price,
                        liability_effect=-payoff,
                        note=note,
                        account_effects=[account_effect(account, net)],
                    )
                )
                continue

            annual_rate = property_item.annual_appreciation_rate
            if annual_rate:
                base_value = self.owned_value(context, property_item)
                growth = cents(base_value * monthly_rate_from_annual_percent(annual_rate))
                if growth:
                    context.real_estate_balances[property_item.id] = base_value + growth
                    context.other_asset_balance += growth
                    state.real_estate_appreciation += growth
                    state.audit_lines.append(
                        projection_line(
                            "Property appreciation",
                            property_item.name,
                            growth,
                            other_asset_effect=growth,
                            note=f"{annual_rate}% annual appreciation assumption",
                        )
                    )

            if property_item.monthly_costs and (self.owned_value(context, property_item) > 0 or retained_niessbrauch):
                account = self.funding_account(context, property_item)
                credit_liquid_account(context, account, -property_item.monthly_costs)
                state.expenses += property_item.monthly_costs
                state.real_estate_costs += property_item.monthly_costs
                note = "Monthly carrying costs"
                if account:
                    note += f", from {account.name}"
                state.audit_lines.append(
                    projection_line(
                        "Property costs",
                        property_item.name,
                        property_item.monthly_costs,
                        cash_effect=-property_item.monthly_costs,
                        note=note,
                        account_effects=[account_effect(account, -property_item.monthly_costs)],
                    )
                )

            if (
                property_item.use == RealEstate.Use.RESIDENCE
                and property_item.saved_monthly_rent
                and (self.owned_value(context, property_item) > 0 or retained_niessbrauch)
            ):
                account = self.funding_account(context, property_item)
                saved_rent = cents(property_item.saved_monthly_rent)
                credit_liquid_account(context, account, saved_rent)
                state.expenses -= saved_rent
                state.real_estate_costs -= saved_rent
                note = "Imputed rent saving for rent-vs-buy comparison"
                if account:
                    note += f", offsets cash flow in {account.name}"
                state.audit_lines.append(
                    projection_line(
                        "Saved rent",
                        property_item.name,
                        saved_rent,
                        cash_effect=saved_rent,
                        note=note,
                        account_effects=[account_effect(account, saved_rent)],
                    )
                )

            if property_item.use == RealEstate.Use.INVESTMENT and property_item.monthly_rent:
                after_vacancy = property_item.monthly_rent * (Decimal("1.00") - property_item.vacancy_rate / Decimal("100"))
                tax = after_vacancy * property_item.rent_tax_rate / Decimal("100")
                net_rent = cents(after_vacancy - tax)
                account = self.funding_account(context, property_item)
                credit_liquid_account(context, account, net_rent)
                state.income += net_rent
                state.rental_income += net_rent
                note = f"{property_item.vacancy_rate}% vacancy, {property_item.rent_tax_rate}% rent tax"
                if account:
                    note += f", to {account.name}"
                state.audit_lines.append(
                    projection_line(
                        "Rental income",
                        property_item.name,
                        net_rent,
                        cash_effect=net_rent,
                        note=note,
                        account_effects=[account_effect(account, net_rent)],
                    )
                )


class DebtContributor:
    def __init__(self, debts):
        self.debts = debts

    def payment_account(self, context, debt):
        return debt.source_account or context.default_operating_account

    def apply(self, context, state):
        for debt in self.debts:
            if not item_applies(debt, state.month):
                continue
            opening_principal = context.debt_balances[debt.id]
            if opening_principal <= 0:
                continue
            monthly_interest_rate, monthly_payment, interest_only = debt_month_plan(debt, state.month)
            interest, payment, principal, new_principal = amortize_month(
                opening_principal, monthly_interest_rate, monthly_payment, interest_only
            )
            operating = self.payment_account(context, debt)
            liability_delta = cents(new_principal - opening_principal)
            context.debt_balances[debt.id] = new_principal
            context.liability_balance += liability_delta
            credit_liquid_account(context, operating, -payment)
            state.expenses += interest
            state.transfers += principal
            state.debt_interest += interest
            state.debt_principal += principal
            note = f"{interest} interest, {principal} principal"
            if interest_only:
                note += " (interest only)"
            elif liability_delta > 0:
                note += f", liability increased by {liability_delta}"
            if operating:
                note += f", from {operating.name}"
            state.audit_lines.append(
                projection_line(
                    "Debt",
                    debt.name,
                    payment,
                    cash_effect=-payment,
                    liability_effect=liability_delta,
                    note=note,
                    account_effects=[account_effect(operating, -payment)],
                )
            )

            extra_payment = debt_extra_payment_for_month(debt, state.month, new_principal)
            if extra_payment:
                context.debt_balances[debt.id] = new_principal - extra_payment
                context.liability_balance -= extra_payment
                credit_liquid_account(context, operating, -extra_payment)
                state.transfers += extra_payment
                state.debt_principal += extra_payment
                extra_note = "Annual Sondertilgung"
                if operating:
                    extra_note += f", from {operating.name}"
                state.audit_lines.append(
                    projection_line(
                        "Extra repayment",
                        debt.name,
                        extra_payment,
                        cash_effect=-extra_payment,
                        liability_effect=-extra_payment,
                        note=extra_note,
                        account_effects=[account_effect(operating, -extra_payment)],
                    )
                )


class InvestmentIncomeContributor:
    def __init__(self, income_investments):
        self.income_investments = income_investments

    def apply(self, context, state):
        for investment in self.income_investments:
            if not investment.is_active:
                continue
            # Capital outlay: a purchase made during the projection pays for itself
            # from the funding account at its start month. Investments owned before
            # the projection start are assumed already paid (no outlay modelled).
            if (
                investment.source_account_id
                and investment.principal
                and first_of_month(investment.start_month) >= context.projection_start
                and state.month == first_of_month(investment.start_month)
            ):
                outlay = investment.principal
                credit_liquid_account(context, investment.source_account, -outlay)
                state.transfers += outlay
                source = investment.source_account.name if investment.source_account else "liquid pool"
                state.audit_lines.append(
                    projection_line(
                        "Investment purchase",
                        investment.name,
                        outlay,
                        cash_effect=-outlay,
                        note=f"Capital paid from {source}",
                        account_effects=[account_effect(investment.source_account, -outlay)],
                    )
                )
            if not item_applies(investment, state.month):
                continue
            growth_rate = effective_income_growth_rate(investment, context.default_income_growth_rate)
            amount = growth_adjusted_amount(
                investment.monthly_income,
                growth_rate,
                investment.start_month,
                state.month,
            )
            operating = context.default_operating_account
            state.income += amount
            state.investment_income += amount
            credit_liquid_account(context, operating, amount)
            note = ""
            if growth_rate:
                note = f"{growth_rate}% annual income growth from {investment.start_month:%Y-%m}"
            state.audit_lines.append(
                projection_line(
                    "Investment income",
                    investment.name,
                    amount,
                    cash_effect=amount,
                    note=note,
                    account_effects=[account_effect(operating, amount)],
                )
            )


class PrivateLoanReceivableContributor:
    def __init__(self, private_loans):
        self.private_loans = private_loans

    def repayment_note(self, loan):
        if loan.source_account_id:
            return f"To {loan.source_account.name}"
        return "To general liquid pool"

    def apply(self, context, state):
        for loan in self.private_loans:
            if not loan.is_active:
                continue
            # Disbursement: a loan paid out during the projection (a future or
            # this-month payout) moves cash out of the source account and becomes
            # a receivable. Loans already lent before the projection start are
            # counted in the opening balances instead (disbursed_before(start)).
            if (
                loan.disbursement_month
                and not loan.disbursed_before(context.projection_start)
                and state.month == first_of_month(loan.disbursement_month)
            ):
                disbursed = loan.current_principal
                if disbursed:
                    credit_liquid_account(context, loan.source_account, -disbursed)
                    source = loan.source_account.name if loan.source_account else "liquid pool"
                    if loan.is_gift:
                        # A gift never comes back: the cash leaves net worth instead of
                        # becoming a receivable (no other-asset, no interest/repayment).
                        state.expenses += disbursed
                        state.audit_lines.append(
                            projection_line(
                                "Private loan gift",
                                loan.name,
                                disbursed,
                                cash_effect=-disbursed,
                                note=f"Gift paid from {source} to {loan.borrower or '-'} (not repaid)",
                                account_effects=[account_effect(loan.source_account, -disbursed)],
                            )
                        )
                    else:
                        context.private_loan_balances[loan.id] = (
                            context.private_loan_balances.get(loan.id, Decimal("0.00")) + disbursed
                        )
                        context.other_asset_balance += disbursed
                        state.transfers += disbursed
                        state.audit_lines.append(
                            projection_line(
                                "Private loan disbursed",
                                loan.name,
                                disbursed,
                                cash_effect=-disbursed,
                                other_asset_effect=disbursed,
                                note=f"Paid out from {source} to {loan.borrower or '-'}",
                                account_effects=[account_effect(loan.source_account, -disbursed)],
                            )
                        )
            if loan.is_gift:
                continue
            if not item_applies(loan, state.month):
                continue
            outstanding = context.private_loan_balances.get(loan.id, Decimal("0.00"))
            if outstanding <= 0:
                continue

            gross_interest = Decimal("0.00")
            allowance_used = Decimal("0.00")
            tax = Decimal("0.00")
            if loan.annual_interest_rate:
                gross_interest = cents(outstanding * loan.annual_interest_rate / Decimal("100") / Decimal("12"))
                interest, tax, allowance_used = apply_capital_tax(context, state, gross_interest, loan.interest_tax_rate)
            else:
                interest = loan.monthly_interest_income
            if interest:
                state.income += interest
                state.investment_income += interest
                credit_liquid_account(context, loan.source_account, interest)
                note = f"Borrower: {loan.borrower or '-'}"
                if loan.annual_interest_rate:
                    note += f", {gross_interest} gross, {allowance_used} allowance, {tax} tax at {loan.interest_tax_rate}%"
                note += f", {self.repayment_note(loan)}"
                state.audit_lines.append(
                    projection_line(
                        "Private loan interest",
                        loan.name,
                        interest,
                        cash_effect=interest,
                        note=note,
                        account_effects=[account_effect(loan.source_account, interest)],
                    )
                )

            final_repayment_month = bool(loan.end_month) and state.month == first_of_month(loan.end_month)
            principal = outstanding if final_repayment_month else min(loan.monthly_principal_repayment, outstanding)
            if principal:
                context.private_loan_balances[loan.id] = outstanding - principal
                credit_liquid_account(context, loan.source_account, principal)
                context.other_asset_balance -= principal
                state.private_loan_principal += principal
                note = f"Remaining receivable: {context.private_loan_balances[loan.id]}, {self.repayment_note(loan)}"
                if final_repayment_month:
                    note = f"Final repayment. {note}"
                state.audit_lines.append(
                    projection_line(
                        "Private loan principal",
                        loan.name,
                        principal,
                        cash_effect=principal,
                        other_asset_effect=-principal,
                        note=note,
                        account_effects=[account_effect(loan.source_account, principal)],
                    )
                )


class SavingsInterestContributor:
    def __init__(self, accounts):
        self.accounts = accounts

    def apply(self, context, state):
        for account in self.accounts:
            if not savings_interest_applies(account, state.index):
                continue
            if context.savings_balances[account.id] <= 0:
                continue
            gross_interest = savings_gross_interest(account, context.savings_balances[account.id])
            net_interest, tax, allowance_used = apply_capital_tax(
                context, state, gross_interest, account.savings_interest_tax_rate
            )
            if not net_interest:
                continue
            context.savings_balances[account.id] += net_interest
            state.income += net_interest
            state.savings_interest_income += net_interest
            context.liquid_balance += net_interest
            state.audit_lines.append(
                projection_line(
                    "Savings interest",
                    account.name,
                    net_interest,
                    cash_effect=net_interest,
                    note=f"{gross_interest} gross, {allowance_used} allowance, {tax} tax at {account.savings_interest_tax_rate}%",
                    account_effects=[account_effect(account, net_interest)],
                )
            )


class DepotDistributionContributor:
    """Models recurring cash distributions/dividends. Depots valued by a flat
    account balance use one blended rate on the account; depots valued by
    summed holdings use each holding's own rate, since not every holding in a
    depot pays out (e.g. an accumulating fund alongside a distributing one).
    A planned investment purchase into a holdings-valued depot gets its own
    rate too, since otherwise the money it adds to the account would be
    invisible to distribution modeling entirely -- or worse, inflate the
    scaled value (and so the modeled payout) of pre-existing holdings that
    the new cash was never actually invested in."""

    def __init__(self, accounts, holdings, purchases=None):
        self.accounts = [account for account in accounts if account.counts_in_household_net_worth]
        self.holdings = [holding for holding in holdings if holding.asset_account.counts_in_household_net_worth]
        self.purchases = [
            purchase
            for purchase in (purchases or [])
            if purchase.is_active
            and purchase.target_account.uses_holdings_valuation
            and purchase.target_account.counts_in_household_net_worth
        ]
        # Captured once, before any contributor mutates context.depot_balances,
        # so holdings can scale their distribution base by how much the
        # account has grown since -- otherwise a holding's static
        # quantity x latest_price would pay the same flat amount forever even
        # as the depot's projected value climbs.
        self.account_baseline = {}
        for holding in self.holdings:
            account_id = holding.asset_account_id
            if account_id not in self.account_baseline:
                self.account_baseline[account_id] = holding.asset_account.effective_balance
        # A purchase's own value, grown independently by the account's price
        # return rate from its purchase month onward. This deliberately does
        # not read context.depot_balances the way holdings do above -- that
        # shared bucket also carries every other contribution to the same
        # account, which would inflate this purchase's distribution base by
        # money it never actually held.
        self.purchase_value = {}

    def apply(self, context, state):
        for account in self.accounts:
            if not depot_distribution_applies(account, state.index):
                continue
            balance = context.depot_balances.get(account.id, account.effective_balance)
            gross = depot_gross_distribution(account, balance)
            self._distribute(
                context,
                state,
                account_id=account.id,
                gross=gross,
                partial_exemption_rate=account.depot_teilfreistellung_rate,
                label=account.name,
                yield_rate=account.depot_annual_distribution_rate,
            )
        for holding in self.holdings:
            if not holding_distribution_applies(holding, state.index):
                continue
            baseline = self.account_baseline.get(holding.asset_account_id, Decimal("0.00"))
            current = context.depot_balances.get(holding.asset_account_id, baseline)
            growth_ratio = (current / baseline) if baseline > 0 else Decimal("1.00")
            scaled_value = holding.current_value * growth_ratio
            gross = holding_gross_distribution(holding, scaled_value)
            self._distribute(
                context,
                state,
                account_id=holding.asset_account_id,
                gross=gross,
                partial_exemption_rate=holding.asset_account.depot_teilfreistellung_rate,
                label=holding.name,
                yield_rate=holding.annual_distribution_rate,
            )
        for purchase in self.purchases:
            purchase_month = first_of_month(purchase.purchase_month)
            if state.month < purchase_month:
                continue
            if purchase.payout_date and state.month > first_of_month(purchase.payout_date):
                continue
            if purchase.id not in self.purchase_value:
                self.purchase_value[purchase.id] = cents(purchase.purchase_amount)
            elif state.month > purchase_month:
                monthly_rate = (
                    context.depot_monthly_return_rates[state.index]
                    if state.index < len(context.depot_monthly_return_rates)
                    else context.depot_growth_rates.get(purchase.target_account_id, Decimal("0.00"))
                )
                if monthly_rate:
                    self.purchase_value[purchase.id] = cents(
                        self.purchase_value[purchase.id] * (Decimal("1.00") + monthly_rate)
                    )
            if not holding_distribution_applies(purchase, state.index):
                continue
            gross = holding_gross_distribution(purchase, self.purchase_value[purchase.id])
            self._distribute(
                context,
                state,
                account_id=purchase.target_account_id,
                gross=gross,
                partial_exemption_rate=purchase.target_account.depot_teilfreistellung_rate,
                label=purchase.name,
                yield_rate=purchase.annual_distribution_rate,
            )

    def _distribute(self, context, state, account_id, gross, partial_exemption_rate, label, yield_rate):
        context.depot_gross_distributions_by_year[(state.month.year, account_id)] = (
            context.depot_gross_distributions_by_year.get((state.month.year, account_id), Decimal("0.00")) + gross
        )
        net_distribution, tax, allowance_used = apply_capital_tax(
            context,
            state,
            gross,
            context.capital_tax_rate,
            partial_exemption_rate=partial_exemption_rate,
        )
        if not net_distribution:
            return
        # Distributions are paid out as cash; the depot value keeps growing at
        # its (price) return rate, so the balance is not reduced here.
        operating = context.default_operating_account
        state.income += net_distribution
        state.depot_income += net_distribution
        credit_liquid_account(context, operating, net_distribution)
        state.audit_lines.append(
            projection_line(
                "Depot distribution",
                label,
                net_distribution,
                cash_effect=net_distribution,
                note=f"{gross} gross, {partial_exemption_rate}% partial exemption, {allowance_used} allowance, {tax} tax at {context.capital_tax_rate}% on {yield_rate}% yield",
                account_effects=[account_effect(operating, net_distribution)],
            )
        )


class RetirementContributor:
    def __init__(self, retirement_plans, household):
        self.retirement_plans = retirement_plans
        self.household = household

    def apply(self, context, state):
        for plan in self.retirement_plans:
            if retirement_contribution_applies(plan, state.month):
                cash_cost = cents(plan.contribution_cash_cost)
                if cash_cost:
                    operating = context.default_operating_account
                    credit_liquid_account(context, operating, -cash_cost)
                    state.transfers += cash_cost
                    note = (
                        f"{plan.get_vehicle_type_display()}, gross contribution {plan.monthly_contribution}, "
                        f"{plan.contribution_relief_rate}% relief"
                    )
                    state.audit_lines.append(
                        projection_line(
                            "Retirement contribution",
                            plan.name,
                            cash_cost,
                            cash_effect=-cash_cost,
                            note=note,
                            account_effects=[account_effect(operating, -cash_cost)],
                        )
                    )
            if not retirement_applies(plan, state.month):
                continue
            deduction_rate = plan.payout_deduction_rate(self.household)
            amount = retirement_monthly_income(
                plan, context.projection_start, state.month, deduction_rate
            )
            operating = context.default_operating_account
            state.income += amount
            state.retirement_income += amount
            credit_liquid_account(context, operating, amount)
            note = f"For {plan.person.name}"
            if deduction_rate:
                note += f", {plan.get_vehicle_type_display()}, net of {deduction_rate}% tax and insurance exposure"
            state.audit_lines.append(
                projection_line(
                    "Retirement income",
                    plan.name,
                    amount,
                    cash_effect=amount,
                    note=note,
                    account_effects=[account_effect(operating, amount)],
                )
            )


class EquityGrantContributor:
    def __init__(self, equity_grants):
        self.equity_grants = equity_grants

    def apply(self, context, state):
        for grant in self.equity_grants:
            if not equity_grant_applies(grant, state.month):
                continue
            amount = cents(grant.net_vest_value)
            account = grant.account or context.default_operating_account
            state.income += amount
            state.equity_income += amount
            credit_liquid_account(context, account, amount)
            note = f"Net vest value after {grant.withholding_rate}% withholding"
            if account:
                note += f", to {account.name}"
            state.audit_lines.append(
                projection_line(
                    "Equity income",
                    grant.name,
                    amount,
                    cash_effect=amount,
                    note=note,
                    account_effects=[account_effect(account, amount)],
                )
            )


class SalaryChangeContributor:
    def __init__(self, people):
        self.people = people

    def apply(self, context, state):
        for person in self.people:
            for change in person.salary_changes.all():
                if not item_applies(change, state.month):
                    continue
                account = change.account or context.default_operating_account
                state.income += change.monthly_net_income_delta
                state.salary_change_income += change.monthly_net_income_delta
                credit_liquid_account(context, account, change.monthly_net_income_delta)
                note = f"For {person.name}"
                if account:
                    note += f", to {account.name}"
                state.audit_lines.append(
                    projection_line(
                        "Salary change",
                        change.name,
                        change.monthly_net_income_delta,
                        cash_effect=change.monthly_net_income_delta,
                        note=note,
                        account_effects=[account_effect(account, change.monthly_net_income_delta)],
                    )
                )


class ChildMilestoneContributor:
    def __init__(self, people):
        self.people = people

    def apply(self, context, state):
        for person in self.people:
            for milestone in person.child_milestones.all():
                if not item_applies(milestone, state.month):
                    continue
                operating = context.default_operating_account
                state.income += milestone.monthly_income_delta
                state.child_income += milestone.monthly_income_delta
                credit_liquid_account(context, operating, milestone.monthly_income_delta)
                state.expenses += milestone.monthly_cost_delta
                state.child_expenses += milestone.monthly_cost_delta
                credit_liquid_account(context, operating, -milestone.monthly_cost_delta)
                if milestone.monthly_income_delta:
                    state.audit_lines.append(
                        projection_line(
                            "Child income",
                            milestone.name,
                            milestone.monthly_income_delta,
                            cash_effect=milestone.monthly_income_delta,
                            note=f"For {person.name}",
                            account_effects=[account_effect(operating, milestone.monthly_income_delta)],
                        )
                    )
                if milestone.monthly_cost_delta:
                    state.audit_lines.append(
                        projection_line(
                            "Child cost",
                            milestone.name,
                            milestone.monthly_cost_delta,
                            cash_effect=-milestone.monthly_cost_delta,
                            note=f"For {person.name}",
                            account_effects=[account_effect(operating, -milestone.monthly_cost_delta)],
                        )
                    )


class TrueExpenseContributor:
    def __init__(self, true_expenses):
        self.true_expenses = true_expenses

    def apply(self, context, state):
        for expense in self.true_expenses:
            if not true_expense_applies(expense, state.month):
                continue
            account = expense.account or context.default_operating_account
            state.expenses += expense.amount
            state.true_expenses += expense.amount
            credit_liquid_account(context, account, -expense.amount)
            note = expense.get_cadence_display()
            if account:
                note += f", from {account.name}"
            state.audit_lines.append(
                projection_line(
                    "True expense",
                    expense.name,
                    expense.amount,
                    cash_effect=-expense.amount,
                    note=note,
                    account_effects=[account_effect(account, -expense.amount)],
                )
            )


class ScenarioContributor:
    def __init__(self, scenario):
        self.scenario = scenario

    def apply(self, context, state):
        scenario = self.scenario
        operating = context.default_operating_account
        state.income += scenario.monthly_income_delta
        state.scenario_income += scenario.monthly_income_delta
        credit_liquid_account(context, operating, scenario.monthly_income_delta)
        state.expenses += scenario.monthly_expense_delta
        state.scenario_expenses += scenario.monthly_expense_delta
        credit_liquid_account(context, operating, -scenario.monthly_expense_delta)
        if scenario.monthly_income_delta:
            state.audit_lines.append(
                projection_line(
                    "Scenario income",
                    scenario.name,
                    scenario.monthly_income_delta,
                    cash_effect=scenario.monthly_income_delta,
                    account_effects=[account_effect(operating, scenario.monthly_income_delta)],
                )
            )
        if scenario.monthly_expense_delta:
            state.audit_lines.append(
                projection_line(
                    "Scenario expense",
                    scenario.name,
                    scenario.monthly_expense_delta,
                    cash_effect=-scenario.monthly_expense_delta,
                    account_effects=[account_effect(operating, -scenario.monthly_expense_delta)],
                    )
                )


class GoalContributionContributor:
    def __init__(self, amount, start_month=None, end_month=None, target_account=None):
        self.amount = cents(amount or Decimal("0.00"))
        self.start_month = first_of_month(start_month) if start_month else None
        self.end_month = first_of_month(end_month) if end_month else None
        self.target_account = target_account

    def applies(self, month):
        if self.amount <= 0:
            return False
        if self.start_month and month < self.start_month:
            return False
        if self.end_month and month > self.end_month:
            return False
        return True

    def apply(self, context, state):
        if not self.applies(state.month):
            return
        note = "Projection-only surplus used by the goal planner"
        if self.target_account and self.target_account.account_type == AssetAccount.AccountType.DEPOT:
            context.depot_balances[self.target_account.id] = context.depot_balances.get(
                self.target_account.id,
                self.target_account.effective_balance,
            ) + self.amount
            context.invested_balance += self.amount
            state.income += self.amount
            state.transfers += self.amount
            state.audit_lines.append(
                projection_line(
                    "Goal contribution",
                    self.target_account.name,
                    self.amount,
                    invested_effect=self.amount,
                    note=note,
                    account_effects=[account_effect(self.target_account, self.amount)],
                )
            )
            return
        account = self.target_account if self.target_account and self.target_account.is_liquid else context.default_operating_account
        credit_liquid_account(context, account, self.amount)
        state.income += self.amount
        state.audit_lines.append(
            projection_line(
                "Goal contribution",
                account.name if account else "Liquid pool",
                self.amount,
                cash_effect=self.amount,
                note=note,
                account_effects=[account_effect(account, self.amount)],
            )
        )


class RuleContributor:
    def __init__(self, rules):
        self.rules = rules

    def routed_account(self, context, rule):
        return rule.account or context.default_operating_account

    def apply(self, context, state):
        for rule in self.rules:
            if not rule_applies(rule, state.month, projection_start=context.projection_start):
                continue
            amount = rule.amount
            note = rule.category
            account = self.routed_account(context, rule)
            if rule.kind == MoneyRule.Kind.INCOME:
                rule_start = first_of_month(rule.start_month) if rule.start_month else context.projection_start
                growth_rate = effective_income_growth_rate(rule, context.default_income_growth_rate)
                amount = growth_adjusted_amount(rule.amount, growth_rate, rule_start, state.month)
                if growth_rate:
                    growth_note = f"{growth_rate}% annual income growth from {rule_start:%Y-%m}"
                    note = f"{rule.category} · {growth_note}" if rule.category else growth_note
                if rule.is_taxable and context.income_tax_rate:
                    gross = amount
                    tax = cents(gross * context.income_tax_rate / Decimal("100"))
                    amount = cents(gross - tax)
                    tax_note = f"net of {context.income_tax_rate}% income tax ({tax} on {gross})"
                    note = f"{note} · {tax_note}" if note else tax_note
                state.income += amount
                state.income_rule_income += amount
                credit_liquid_account(context, account, amount)
                if account:
                    account_note = f"to {account.name}"
                    note = f"{note} · {account_note}" if note else account_note
                state.audit_lines.append(
                    projection_line(
                        "Income rule",
                        rule.name,
                        amount,
                        cash_effect=amount,
                        note=note,
                        account_effects=[account_effect(account, amount)],
                    )
                )
            else:
                state.expenses += amount
                credit_liquid_account(context, account, -amount)
                if account:
                    account_note = f"from {account.name}"
                    note = f"{note} · {account_note}" if note else account_note
                state.audit_lines.append(
                    projection_line(
                        "Expense rule",
                        rule.name,
                        amount,
                        cash_effect=-amount,
                        note=note,
                        account_effects=[account_effect(account, -amount)],
                    )
                )


class TransferRuleContributor:
    def __init__(self, transfer_rules):
        self.transfer_rules = transfer_rules

    def debit_source_account(self, context, rule, amount):
        if not rule.source_account_id:
            return "From general liquid pool"
        if rule.source_account.account_type == AssetAccount.AccountType.CASH:
            context.cash_balances[rule.source_account_id] = context.cash_balances.get(
                rule.source_account_id,
                rule.source_account.effective_balance,
            ) - amount
        elif rule.source_account.account_type == AssetAccount.AccountType.SAVINGS:
            context.savings_balances[rule.source_account_id] = context.savings_balances.get(
                rule.source_account_id,
                rule.source_account.effective_balance,
            ) - amount
        return f"From {rule.source_account.name}"

    def apply(self, context, state):
        for rule in self.transfer_rules:
            if not rule_applies(rule, state.month, projection_start=context.projection_start):
                continue
            amount = rule.amount
            if rule.target_account.account_type == AssetAccount.AccountType.DEPOT:
                source_note = self.debit_source_account(context, rule, amount)
                state.transfers += amount
                context.liquid_balance -= amount
                context.depot_balances[rule.target_account_id] = context.depot_balances.get(
                    rule.target_account_id,
                    rule.target_account.effective_balance,
                ) + amount
                context.invested_balance += amount
                state.audit_lines.append(
                    projection_line(
                        "Transfer",
                        rule.name,
                        amount,
                        cash_effect=-amount,
                        invested_effect=amount,
                        note=f"{source_note} to {rule.target_account.name}",
                        account_effects=[
                            account_effect(rule.source_account, -amount),
                            account_effect(rule.target_account, amount),
                        ],
                    )
                )
            elif rule.target_account.account_type == AssetAccount.AccountType.LOAN:
                # Extra repayment. For a debt-backed loan, pay down the
                # amortizing principal so future interest is reduced, and cap
                # the payment at the outstanding balance so we neither spend
                # cash on nor over-credit a loan that is already paid off.
                linked_debt = context.debt_by_account_id.get(rule.target_account_id)
                if linked_debt is not None:
                    outstanding = max(context.debt_balances.get(linked_debt.id, Decimal("0.00")), Decimal("0.00"))
                    applied = min(amount, outstanding)
                    context.debt_balances[linked_debt.id] = outstanding - applied
                else:
                    applied = amount
                if applied:
                    source_note = self.debit_source_account(context, rule, applied)
                    state.transfers += applied
                    context.liquid_balance -= applied
                    context.liability_balance -= applied
                    state.debt_principal += applied
                    state.audit_lines.append(
                        projection_line(
                            "Extra repayment",
                            rule.name,
                            applied,
                            cash_effect=-applied,
                            liability_effect=-applied,
                            note=f"{source_note} to {rule.target_account.name}",
                            account_effects=[
                                account_effect(rule.source_account, -applied),
                                account_effect(rule.target_account, applied),
                            ],
                        )
                    )
            elif rule.target_account.account_type == AssetAccount.AccountType.SAVINGS:
                source_note = self.debit_source_account(context, rule, amount)
                context.savings_balances[rule.target_account_id] = context.savings_balances.get(
                    rule.target_account_id,
                    rule.target_account.effective_balance,
                ) + amount
                state.audit_lines.append(
                    projection_line(
                        "Transfer",
                        rule.name,
                        amount,
                        note=f"{source_note} to {rule.target_account.name}",
                        account_effects=[
                            account_effect(rule.source_account, -amount),
                            account_effect(rule.target_account, amount),
                        ],
                    )
                )
            elif rule.target_account.account_type == AssetAccount.AccountType.CASH:
                source_note = self.debit_source_account(context, rule, amount)
                context.cash_balances[rule.target_account_id] = context.cash_balances.get(
                    rule.target_account_id,
                    rule.target_account.effective_balance,
                ) + amount
                state.audit_lines.append(
                    projection_line(
                        "Transfer",
                        rule.name,
                        amount,
                        note=f"{source_note} to {rule.target_account.name}",
                        account_effects=[
                            account_effect(rule.source_account, -amount),
                            account_effect(rule.target_account, amount),
                        ],
                    )
                )


class PlannedInvestmentPurchaseContributor:
    def __init__(self, purchases):
        self.purchases = purchases

    def debit_source_account(self, context, purchase, amount):
        if not purchase.source_account_id:
            return "From general liquid pool", ()
        if purchase.source_account.account_type == AssetAccount.AccountType.CASH:
            context.cash_balances[purchase.source_account_id] = context.cash_balances.get(
                purchase.source_account_id,
                purchase.source_account.effective_balance,
            ) - amount
        elif purchase.source_account.account_type == AssetAccount.AccountType.SAVINGS:
            context.savings_balances[purchase.source_account_id] = context.savings_balances.get(
                purchase.source_account_id,
                purchase.source_account.effective_balance,
            ) - amount
        return f"From {purchase.source_account.name}", (account_effect(purchase.source_account, -amount),)

    def apply_purchase(self, context, state, purchase):
        amount = cents(purchase.purchase_amount)
        if amount <= 0:
            return
        source_note, source_effects = self.debit_source_account(context, purchase, amount)
        context.liquid_balance -= amount
        context.depot_balances[purchase.target_account_id] = context.depot_balances.get(
            purchase.target_account_id,
            purchase.target_account.effective_balance,
        ) + amount
        context.invested_balance += amount
        state.transfers += amount
        detail = purchase.get_asset_type_display()
        if purchase.isin:
            detail += f" {purchase.isin}"
        state.audit_lines.append(
            projection_line(
                "Planned investment purchase",
                purchase.name,
                amount,
                cash_effect=-amount,
                invested_effect=amount,
                note=f"{source_note} to {purchase.target_account.name}; {detail}",
                account_effects=source_effects + (account_effect(purchase.target_account, amount),),
            )
        )

    def apply_payout(self, context, state, purchase):
        if not purchase.payout_date:
            return
        if first_of_month(purchase.payout_date) != state.month:
            return
        if first_of_month(purchase.purchase_month) > state.month:
            return
        payout = cents(purchase.expected_payout_amount)
        invested_release = cents(purchase.purchase_amount)
        if payout <= 0:
            return
        depot_balance = context.depot_balances.get(
            purchase.target_account_id,
            purchase.target_account.effective_balance,
        )
        invested_release = min(invested_release, max(depot_balance, Decimal("0.00")))
        net_payout, tax, allowance_used, taxable_gain = net_capital_payout(
            context,
            state,
            payout,
            invested_release,
            partial_exemption_rate=purchase.target_account.depot_teilfreistellung_rate,
        )
        operating = context.default_operating_account
        context.depot_balances[purchase.target_account_id] = depot_balance - invested_release
        context.invested_balance -= invested_release
        credit_liquid_account(context, operating, net_payout)
        state.depot_payout += net_payout
        gain_or_loss = cents(payout - invested_release)
        note = f"Payout/maturity date {purchase.payout_date:%Y-%m-%d}"
        if gain_or_loss:
            note += f", expected return since purchase {gain_or_loss}"
        if tax:
            note += (
                f", {taxable_gain} taxable gain, {purchase.target_account.depot_teilfreistellung_rate}% "
                f"partial exemption, {allowance_used} allowance, {tax} capital tax"
            )
        state.audit_lines.append(
            projection_line(
                "Planned investment payout",
                purchase.name,
                net_payout,
                cash_effect=net_payout,
                invested_effect=-invested_release,
                note=note,
                account_effects=[
                    account_effect(purchase.target_account, -invested_release),
                    account_effect(operating, net_payout),
                ],
            )
        )

    def apply(self, context, state):
        for purchase in self.purchases:
            if not purchase.is_active:
                continue
            if first_of_month(purchase.purchase_month) == state.month:
                self.apply_purchase(context, state, purchase)
            self.apply_payout(context, state, purchase)


class FamilyGiftContributor:
    def __init__(self, gifts):
        self.gifts = gifts

    def credit_target_account(self, context, gift, amount):
        account = gift.target_account
        if account.account_type == AssetAccount.AccountType.CASH:
            context.cash_balances[account.id] = context.cash_balances.get(account.id, account.effective_balance) + amount
            if account.counts_in_household_net_worth:
                context.liquid_balance += amount
        elif account.account_type == AssetAccount.AccountType.SAVINGS:
            context.savings_balances[account.id] = context.savings_balances.get(account.id, account.effective_balance) + amount
            if account.counts_in_household_net_worth:
                context.liquid_balance += amount
        elif account.account_type == AssetAccount.AccountType.DEPOT:
            context.depot_balances[account.id] = context.depot_balances.get(account.id, account.effective_balance) + amount
            if account.counts_in_household_net_worth:
                context.invested_balance += amount
        elif account.account_type == AssetAccount.AccountType.OTHER and account.counts_in_household_net_worth:
            context.other_asset_balance += amount

    def apply(self, context, state):
        for gift in self.gifts:
            if not gift.is_active or first_of_month(gift.gift_month) != state.month:
                continue
            amount = cents(gift.amount)
            if amount <= 0:
                continue
            debit_liquid_account(context, gift.source_account, amount)
            self.credit_target_account(context, gift, amount)
            state.transfers += amount
            window = f"{gift.window_start_year}-{gift.window_end_year}"
            note = (
                f"{gift.giver.name} to {gift.recipient.name}; "
                f"{window} allowance {gift.allowance_amount}"
            )
            if gift.purpose:
                note += f"; {gift.purpose}"
            state.audit_lines.append(
                projection_line(
                    "Family gift",
                    gift.name,
                    amount,
                    cash_effect=-amount,
                    note=note,
                    account_effects=[
                        account_effect(gift.source_account, -amount),
                        account_effect(gift.target_account, amount),
                    ],
                )
            )


class RetirementDepotDrawContributor:
    """Opt-in (Household.fund_cash_goal_from_depot). Runs last each month:
    1. Treat the yearly cash goal as household spending (an outflow).
    2. Cover any resulting negative liquidity by selling from the depot, net of
       the capital-gains rate. Drawing is capital, not income, so it feeds
       ``depot_draw`` / net cash flow rather than the income components."""

    def __init__(self, accounts, cash_goals, annual_inflation_rate=None, cash_goal_multiplier=Decimal("1.00")):
        self.depot_accounts = [
            account for account in accounts if account.account_type == AssetAccount.AccountType.DEPOT
        ]
        self.depot_account_ids = [
            account.id for account in self.depot_accounts
        ]
        self.cash_goals = list(cash_goals)
        self.annual_inflation_rate = annual_inflation_rate
        self.cash_goal_multiplier = cash_goal_multiplier

    def weighted_exemption_rate(self, context):
        weighted_value = Decimal("0.00")
        total_value = Decimal("0.00")
        for account in self.depot_accounts:
            balance = context.depot_balances.get(account.id, Decimal("0.00"))
            if balance <= 0:
                continue
            total_value += balance
            weighted_value += balance * account.depot_teilfreistellung_rate
        if total_value <= 0:
            return Decimal("0.00")
        return cents(weighted_value / total_value)

    def apply(self, context, state):
        goal = cash_goal_for_year(state.month.year, self.cash_goals)
        monthly_goal = (
            cents(
                cash_goal_amount_for_year(
                    goal,
                    state.month.year,
                    annual_inflation_rate=self.annual_inflation_rate,
                    multiplier=self.cash_goal_multiplier,
                )
                / Decimal("12")
            )
            if goal
            else Decimal("0.00")
        )
        operating = context.default_operating_account
        if monthly_goal > 0:
            credit_liquid_account(context, operating, -monthly_goal)
            state.expenses += monthly_goal
            state.audit_lines.append(
                projection_line(
                    "Cash goal spending",
                    goal.name,
                    monthly_goal,
                    cash_effect=-monthly_goal,
                    note="Planned household spending funded by income and depot draw",
                    account_effects=[account_effect(operating, -monthly_goal)],
                )
            )

        if context.liquid_balance >= 0:
            return
        shortfall = -context.liquid_balance
        total_depot = sum(
            (context.depot_balances.get(account_id, Decimal("0.00")) for account_id in self.depot_account_ids),
            Decimal("0.00"),
        )
        if total_depot <= 0:
            return

        weighted_exemption_rate = self.weighted_exemption_rate(context)
        gross = gross_for_net_after_capital_tax(
            context,
            state,
            shortfall,
            context.capital_tax_rate,
            partial_exemption_rate=weighted_exemption_rate,
        )
        gross = min(gross, total_depot)
        remaining = gross
        for account_id in self.depot_account_ids:
            if remaining <= 0:
                break
            balance = context.depot_balances.get(account_id, Decimal("0.00"))
            take = min(balance, remaining)
            context.depot_balances[account_id] = balance - take
            remaining -= take
        net, tax, allowance_used = apply_capital_tax(
            context,
            state,
            gross,
            context.capital_tax_rate,
            partial_exemption_rate=weighted_exemption_rate,
        )
        context.invested_balance -= gross
        credit_liquid_account(context, operating, net)
        state.depot_draw += net
        state.audit_lines.append(
            projection_line(
                "Depot draw",
                "Portfolio",
                net,
                cash_effect=net,
                invested_effect=-gross,
                note=f"Sold {gross} depot, {weighted_exemption_rate}% partial exemption, {allowance_used} allowance, {tax} capital-gains tax at {context.capital_tax_rate}%",
                account_effects=[account_effect(operating, net)],
            )
        )


def build_projection(household, scenario=None, stress=None):
    stress = stress or {}
    start = first_of_month(household.start_month)
    accounts = list(household.accounts.all())
    planning_accounts = [account for account in accounts if account.counts_in_household_net_worth]
    depot_payout_holdings = list(
        DepotHolding.objects.filter(
            asset_account__household=household,
            payout_date__isnull=False,
        ).select_related("asset_account")
    )
    depot_distribution_holdings = list(
        DepotHolding.objects.filter(
            asset_account__household=household,
            asset_account__depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
            annual_distribution_rate__gt=0,
        ).select_related("asset_account")
    )
    debts = list(household.debts.select_related("account", "source_account"))
    income_investments = list(household.income_investments.select_related("source_account"))
    private_loans = list(household.private_loans.select_related("source_account"))
    properties = list(
        household.properties.select_related("source_account", "sale_proceeds_account").prefetch_related("debts")
    )
    sell_real_estate_month = stress.get("sell_real_estate_month")
    if sell_real_estate_month:
        sale_month = first_of_month(sell_real_estate_month)
        for property_item in properties:
            if property_item.is_active and property_item.acquired_before(start):
                property_item.sale_month = sale_month
    real_estate_transfer_plans = list(
        household.real_estate_transfer_plans.select_related("property_item", "giver", "recipient")
    )
    if stress.get("disable_real_estate_transfers"):
        real_estate_transfer_plans = []
    retirement_plans = list(household.retirement_plans.select_related("person"))
    pension_adjustment_override = stress.get("pension_adjustment_override")
    if pension_adjustment_override is not None:
        for plan in retirement_plans:
            plan.annual_adjustment_rate = pension_adjustment_override
    equity_grants = list(household.equity_grants.select_related("person", "account"))
    true_expenses = list(household.true_expenses.select_related("account"))
    child_milestones = list(
        household.people.filter(role="child").prefetch_related("child_milestones")
    )
    salary_changes = list(
        household.people.filter(role="adult").prefetch_related("salary_changes__account")
    )
    # A future property's loans don't exist until it's acquired, so their opening
    # balance is deferred to 0 (seeded by the RealEstateContributor at acquisition).
    future_property_mortgage_ids = {
        debt.id
        for item in properties
        if item.is_active and not item.acquired_before(start)
        for debt in item.debts.all()
    }
    debt_account_ids = {debt.account_id for debt in debts}
    debt_rate_delta = stress.get("debt_rate_delta", Decimal("0.00"))
    if debt_rate_delta:
        for debt in debts:
            debt.annual_interest_rate += debt_rate_delta
            if debt.refinance_annual_interest_rate is not None:
                debt.refinance_annual_interest_rate += debt_rate_delta
    debt_by_account_id = {debt.account_id: debt for debt in debts}
    debt_balances = {
        debt.id: (Decimal("0.00") if debt.id in future_property_mortgage_ids else debt.current_principal)
        for debt in debts
    }
    savings_balances = {
        account.id: account.effective_balance
        for account in accounts
        if account.account_type == AssetAccount.AccountType.SAVINGS
    }
    depot_balances = {
        account.id: account.effective_balance
        for account in accounts
        if account.account_type == AssetAccount.AccountType.DEPOT
    }
    cash_balances = {
        account.id: account.effective_balance
        for account in accounts
        if account.account_type == AssetAccount.AccountType.CASH
    }
    private_loan_balances = {
        loan.id: loan.current_principal
        for loan in private_loans
        if loan.is_active and not loan.is_gift and loan.disbursed_before(start)
    }
    private_loan_total = sum(private_loan_balances.values(), Decimal("0.00"))
    real_estate_balances = {
        item.id: item.current_value
        for item in properties
        if item.is_active and item.acquired_before(start)
    }
    real_estate_total = sum(real_estate_balances.values(), Decimal("0.00"))
    if accounts:
        # Single-currency assumption: balances are summed without FX conversion,
        # so every account/holding must be in the household currency. Forms
        # enforce this and the quality report flags any drift (e.g. from imports).
        liquid_balance = sum((account.effective_balance for account in planning_accounts if account.is_liquid), Decimal("0.00"))
        invested_balance = sum(
            (
                depot_balances[account.id]
                for account in planning_accounts
                if account.account_type == AssetAccount.AccountType.DEPOT
            ),
            Decimal("0.00"),
        )
        other_asset_balance = sum((account.effective_balance for account in planning_accounts if account.is_other_asset), Decimal("0.00")) + private_loan_total + real_estate_total
        liability_balance = sum(
            (
                account.effective_balance
                for account in planning_accounts
                if account.account_type == AssetAccount.AccountType.LOAN and account.id not in debt_account_ids
            ),
            Decimal("0.00"),
        ) + sum(debt_balances.values(), Decimal("0.00"))
    else:
        liquid_balance = household.starting_balance
        invested_balance = Decimal("0.00")
        other_asset_balance = Decimal("0.00")
        liability_balance = Decimal("0.00")
        other_asset_balance += private_loan_total + real_estate_total
    if scenario and scenario.is_active:
        liquid_balance += scenario.liquid_balance_delta
    rules = list(household.rules.select_related("person", "account"))
    transfer_rules = list(household.transfer_rules.select_related("person", "source_account", "target_account"))
    planned_purchases = list(
        household.planned_investment_purchases.select_related("person", "source_account", "target_account")
    )
    family_gifts = list(
        household.family_gift_plans.select_related("giver", "recipient", "source_account", "target_account")
    )
    if stress.get("disable_family_gifts"):
        family_gifts = []
    goal_target_account = None
    goal_target_account_id = stress.get("goal_target_account_id")
    if goal_target_account_id:
        goal_target_account = next((account for account in accounts if account.id == goal_target_account_id), None)
    retirement_deduction_rate = (
        household.pension_tax_rate
        + household.health_insurance_rate
        + household.church_tax_rate
        + household.solidarity_surcharge_rate
    )
    capital_tax_rate = (
        household.capital_gains_tax_rate
        + household.church_tax_rate
        + household.solidarity_surcharge_rate
    )
    depot_growth_override = stress.get("depot_annual_return_override")
    depot_growth_rates = {}
    for account in accounts:
        if account.account_type != AssetAccount.AccountType.DEPOT:
            continue
        annual_rate = depot_growth_override if depot_growth_override is not None else account.depot_annual_return_rate
        if annual_rate:
            depot_growth_rates[account.id] = monthly_rate_from_annual_percent(annual_rate)

    context = ProjectionContext(
        projection_start=start,
        liquid_balance=liquid_balance,
        invested_balance=invested_balance,
        other_asset_balance=other_asset_balance,
        liability_balance=liability_balance,
        debt_balances=debt_balances,
        cash_balances=cash_balances,
        savings_balances=savings_balances,
        depot_balances=depot_balances,
        depot_year_opening_balances={},
        depot_gross_distributions_by_year={},
        private_loan_balances=private_loan_balances,
        real_estate_balances=real_estate_balances,
        debt_by_account_id=debt_by_account_id,
        depot_growth_rates=depot_growth_rates,
        depot_monthly_return_rates=stress.get("depot_monthly_return_rates", []),
        default_operating_account=household.default_operating_account,
        default_income_growth_rate=household.default_income_growth_rate,
        retirement_deduction_rate=retirement_deduction_rate,
        capital_tax_rate=capital_tax_rate,
        income_tax_rate=household.income_tax_rate,
        capital_income_allowance=household.capital_income_allowance,
        capital_allowance_used={},
        vorabpauschale_basiszins_rate=household.vorabpauschale_basiszins_rate,
    )
    contributors = [
        DepotVorabpauschaleContributor(accounts),
        DepotGrowthContributor(accounts),
        DepotPayoutContributor(depot_payout_holdings),
        RealEstateContributor(properties, real_estate_transfer_plans),
        DebtContributor(debts),
        InvestmentIncomeContributor(income_investments),
        PrivateLoanReceivableContributor(private_loans),
        SavingsInterestContributor(accounts),
        DepotDistributionContributor(accounts, depot_distribution_holdings, planned_purchases),
        RetirementContributor(retirement_plans, household),
        EquityGrantContributor(equity_grants),
        SalaryChangeContributor(salary_changes),
        ChildMilestoneContributor(child_milestones),
        TrueExpenseContributor(true_expenses),
    ]
    if scenario and scenario.is_active:
        contributors.append(ScenarioContributor(scenario))
    if stress.get("goal_monthly_contribution"):
        contributors.append(
            GoalContributionContributor(
                stress.get("goal_monthly_contribution"),
                start_month=stress.get("goal_contribution_start"),
                end_month=stress.get("goal_contribution_end"),
                target_account=goal_target_account,
            )
        )
    contributors.append(RuleContributor(rules))
    contributors.append(TransferRuleContributor(transfer_rules))
    contributors.append(FamilyGiftContributor(family_gifts))
    contributors.append(PlannedInvestmentPurchaseContributor(planned_purchases))
    if household.fund_cash_goal_from_depot:
        contributors.append(
            RetirementDepotDrawContributor(
                accounts,
                household.cash_goals.all(),
                annual_inflation_rate=stress.get("annual_inflation_rate"),
                cash_goal_multiplier=stress.get("cash_goal_multiplier", Decimal("1.00")),
            )
        )

    months = []
    for index in range(household.projection_months):
        month = add_months(start, index)
        opening_liquid_balance = context.liquid_balance
        opening_invested_balance = context.invested_balance
        opening_other_asset_balance = context.other_asset_balance
        opening_liability_balance = context.liability_balance
        opening_net_worth = (
            opening_liquid_balance
            + opening_invested_balance
            + opening_other_asset_balance
            - opening_liability_balance
        )
        state = MonthState(index=index, month=month)
        for contributor in contributors:
            contributor.apply(context, state)

        net = (
            state.income
            + state.private_loan_principal
            + state.depot_payout
            + state.depot_draw
            + state.real_estate_sale_proceeds
            - state.expenses
            - state.transfers
        )
        net_worth = (
            context.liquid_balance
            + context.invested_balance
            + context.other_asset_balance
            - context.liability_balance
        )
        months.append(
            ProjectionMonth(
                index=index,
                month=month,
                opening_liquid_balance=opening_liquid_balance,
                opening_invested_balance=opening_invested_balance,
                opening_other_asset_balance=opening_other_asset_balance,
                opening_liability_balance=opening_liability_balance,
                opening_net_worth=opening_net_worth,
                income=state.income,
                investment_income=state.investment_income,
                depot_growth=state.depot_growth,
                depot_payout=state.depot_payout,
                depot_draw=state.depot_draw,
                depot_income=state.depot_income,
                savings_interest_income=state.savings_interest_income,
                retirement_income=state.retirement_income,
                equity_income=state.equity_income,
                private_loan_principal=state.private_loan_principal,
                real_estate_appreciation=state.real_estate_appreciation,
                real_estate_costs=state.real_estate_costs,
                real_estate_sale_proceeds=state.real_estate_sale_proceeds,
                rental_income=state.rental_income,
                salary_change_income=state.salary_change_income,
                child_income=state.child_income,
                expenses=state.expenses,
                true_expenses=state.true_expenses,
                child_expenses=state.child_expenses,
                scenario_income=state.scenario_income,
                income_rule_income=state.income_rule_income,
                scenario_expenses=state.scenario_expenses,
                transfers=state.transfers,
                debt_interest=state.debt_interest,
                debt_principal=state.debt_principal,
                net=net,
                liquid_balance=context.liquid_balance,
                invested_balance=context.invested_balance,
                other_asset_balance=context.other_asset_balance,
                liability_balance=context.liability_balance,
                balance=context.liquid_balance,
                net_worth=net_worth,
                account_balances=account_balance_snapshot(context, accounts),
                audit_lines=state.audit_lines,
            )
        )

    return months


def _sum_decimal(items, field_name):
    return sum((getattr(item, field_name) for item in items), Decimal("0.00"))


def aggregate_audit_lines(months):
    aggregate = {}
    counts = {}
    for month in months:
        for line in month.audit_lines:
            key = (line.section, line.name)
            if key not in aggregate:
                aggregate[key] = {
                    "amount": Decimal("0.00"),
                    "cash_effect": Decimal("0.00"),
                    "invested_effect": Decimal("0.00"),
                    "other_asset_effect": Decimal("0.00"),
                    "liability_effect": Decimal("0.00"),
                    "account_effects": {},
                }
                counts[key] = 0
            aggregate[key]["amount"] += line.amount
            aggregate[key]["cash_effect"] += line.cash_effect
            aggregate[key]["invested_effect"] += line.invested_effect
            aggregate[key]["other_asset_effect"] += line.other_asset_effect
            aggregate[key]["liability_effect"] += line.liability_effect
            for effect in line.account_effects:
                account_id = effect["account_id"]
                if account_id not in aggregate[key]["account_effects"]:
                    aggregate[key]["account_effects"][account_id] = {
                        "account_id": account_id,
                        "account_name": effect["account_name"],
                        "account_type": effect["account_type"],
                        "amount": Decimal("0.00"),
                    }
                aggregate[key]["account_effects"][account_id]["amount"] += effect["amount"]
            counts[key] += 1

    lines = []
    for section, name in sorted(aggregate):
        values = aggregate[(section, name)]
        lines.append(
            projection_line(
                section,
                name,
                values["amount"],
                cash_effect=values["cash_effect"],
                invested_effect=values["invested_effect"],
                other_asset_effect=values["other_asset_effect"],
                liability_effect=values["liability_effect"],
                note=f"Aggregated from {counts[(section, name)]} applied lines.",
                account_effects=values["account_effects"].values(),
            )
        )
    return lines


def build_yearly_projection(
    projection,
    cash_goals=None,
    annual_inflation_rate=None,
    cash_goal_multiplier=Decimal("1.00"),
):
    cash_goals = list(cash_goals or [])
    years = []
    # Bucket by calendar year (not fixed 12-month windows from the start month) so
    # labels are real years and align with the German tax year. A non-January
    # start makes the first bucket partial, and a non-December end makes the last
    # bucket partial; _build_projection_year marks those with a month count.
    for _calendar_year, year_group in groupby(projection, key=lambda item: item.month.year):
        year_months = list(year_group)
        years.append(
            _build_projection_year(
                year_months,
                cash_goals,
                annual_inflation_rate=annual_inflation_rate,
                cash_goal_multiplier=cash_goal_multiplier,
            )
        )

    return years


def _build_projection_year(
    months,
    cash_goals=None,
    annual_inflation_rate=None,
    cash_goal_multiplier=Decimal("1.00"),
):
    cash_goals = cash_goals or []
    first_month = months[0]
    last_month = months[-1]
    # Each bucket is one calendar year; a partial first/last year is flagged so its
    # part-year flow sums (income, expenses, draw) aren't mistaken for a full year.
    calendar_year = first_month.month.year
    if len(months) >= 12:
        label = str(calendar_year)
    else:
        label = f"{calendar_year} ({len(months)} mo)"
    income = _sum_decimal(months, "income")
    # Attribute the cash goal month by month against each month's own calendar
    # year, so a 12-month bucket that straddles two calendar years (any non-
    # January projection start) splits the goal correctly and picks up goals
    # that start, end, or re-index partway through the bucket. For a full
    # calendar-year bucket this sums back to that year's annual goal.
    annual_cash_goal = Decimal("0.00")
    for projection_month in months:
        month_year = projection_month.month.year
        month_goal = cash_goal_for_year(month_year, cash_goals)
        annual_cash_goal += (
            cash_goal_amount_for_year(
                month_goal,
                month_year,
                annual_inflation_rate=annual_inflation_rate,
                multiplier=cash_goal_multiplier,
            )
            / Decimal("12")
        )
    annual_cash_goal = cents(annual_cash_goal)
    cash_goal_gap = max(annual_cash_goal - income, Decimal("0.00"))
    cash_goal_coverage_percent = Decimal("0.00")
    if annual_cash_goal:
        cash_goal_coverage_percent = percent(income / annual_cash_goal * Decimal("100"))
    portfolio_draw_percent = Decimal("0.00")
    if cash_goal_gap and first_month.opening_invested_balance:
        portfolio_draw_percent = percent(cash_goal_gap / first_month.opening_invested_balance * Decimal("100"))
    return ProjectionYear(
        year=first_month.month.year,
        label=label,
        start_index=first_month.index,
        end_index=last_month.index,
        month_count=len(months),
        opening_liquid_balance=first_month.opening_liquid_balance,
        opening_invested_balance=first_month.opening_invested_balance,
        opening_other_asset_balance=first_month.opening_other_asset_balance,
        opening_liability_balance=first_month.opening_liability_balance,
        opening_net_worth=first_month.opening_net_worth,
        income=income,
        investment_income=_sum_decimal(months, "investment_income"),
        depot_growth=_sum_decimal(months, "depot_growth"),
        depot_payout=_sum_decimal(months, "depot_payout"),
        depot_draw=_sum_decimal(months, "depot_draw"),
        depot_income=_sum_decimal(months, "depot_income"),
        savings_interest_income=_sum_decimal(months, "savings_interest_income"),
        retirement_income=_sum_decimal(months, "retirement_income"),
        equity_income=_sum_decimal(months, "equity_income"),
        private_loan_principal=_sum_decimal(months, "private_loan_principal"),
        real_estate_appreciation=_sum_decimal(months, "real_estate_appreciation"),
        real_estate_costs=_sum_decimal(months, "real_estate_costs"),
        real_estate_sale_proceeds=_sum_decimal(months, "real_estate_sale_proceeds"),
        rental_income=_sum_decimal(months, "rental_income"),
        salary_change_income=_sum_decimal(months, "salary_change_income"),
        child_income=_sum_decimal(months, "child_income"),
        expenses=_sum_decimal(months, "expenses"),
        true_expenses=_sum_decimal(months, "true_expenses"),
        child_expenses=_sum_decimal(months, "child_expenses"),
        scenario_income=_sum_decimal(months, "scenario_income"),
        income_rule_income=_sum_decimal(months, "income_rule_income"),
        scenario_expenses=_sum_decimal(months, "scenario_expenses"),
        transfers=_sum_decimal(months, "transfers"),
        debt_interest=_sum_decimal(months, "debt_interest"),
        debt_principal=_sum_decimal(months, "debt_principal"),
        net=_sum_decimal(months, "net"),
        annual_cash_goal=annual_cash_goal,
        cash_goal_coverage_percent=cash_goal_coverage_percent,
        cash_goal_gap=cash_goal_gap,
        portfolio_draw_percent=portfolio_draw_percent,
        ending_liquid_balance=last_month.liquid_balance,
        ending_invested_balance=last_month.invested_balance,
        ending_other_asset_balance=last_month.other_asset_balance,
        ending_liability_balance=last_month.liability_balance,
        ending_net_worth=last_month.net_worth,
        lowest_liquid_balance=min(item.liquid_balance for item in months),
        stress_months=sum(1 for item in months if item.liquid_balance < 0 <= item.net_worth),
        audit_lines=aggregate_audit_lines(months),
    )
