from decimal import Decimal

from .finance import quantize_money as money_value


def gross_capital_draw_for_net(net_needed, tax_rate, allowance):
    net_needed = money_value(net_needed)
    if net_needed <= 0:
        return Decimal("0.00")
    tax_fraction = tax_rate / Decimal("100")
    if tax_fraction >= Decimal("1.00"):
        return net_needed
    allowance = max(money_value(allowance), Decimal("0.00"))
    if net_needed <= allowance:
        return net_needed
    return money_value((net_needed - (tax_fraction * allowance)) / (Decimal("1.00") - tax_fraction))


def retirement_tax_summary(item, household):
    retirement_deduction_rate = (
        household.pension_tax_rate
        + household.health_insurance_rate
        + household.church_tax_rate
        + household.solidarity_surcharge_rate
    )
    capital_tax_rate = household.capital_gains_tax_rate + household.church_tax_rate + household.solidarity_surcharge_rate
    # Retirement income in the projection is already net of pension tax and
    # health insurance, so net income is taken as-is. The pension deduction is
    # reconstructed (gross = net / (1 - rate)) for display only and is NOT
    # subtracted from income again.
    net_retirement_income = max(item.retirement_income, Decimal("0.00"))
    deduction_fraction = retirement_deduction_rate / Decimal("100")
    if net_retirement_income and deduction_fraction < Decimal("1.00"):
        gross_retirement_income = net_retirement_income / (Decimal("1.00") - deduction_fraction)
        retirement_deductions = money_value(gross_retirement_income - net_retirement_income)
    else:
        retirement_deductions = Decimal("0.00")
    net_income = max(item.income, Decimal("0.00"))
    net_cash_gap = max(item.annual_cash_goal - net_income, Decimal("0.00"))
    # Gross up so the draw nets the cash gap after tax: gross = net / (1 - rate),
    # not net * (1 + rate). The latter understates the withdrawal (e.g. netting
    # 1,000 at 25% needs 1,333, not 1,250). Tax is applied to the whole draw as a
    # simple planning assumption.
    tax_fraction = capital_tax_rate / Decimal("100")
    if net_cash_gap and tax_fraction < Decimal("1.00"):
        gross_draw_for_net_cash = gross_capital_draw_for_net(
            net_cash_gap,
            capital_tax_rate,
            household.capital_income_allowance,
        )
    else:
        gross_draw_for_net_cash = net_cash_gap
    capital_tax_drag = gross_draw_for_net_cash - net_cash_gap
    tax_aware_draw_percent = Decimal("0.00")
    if gross_draw_for_net_cash and item.opening_invested_balance:
        tax_aware_draw_percent = money_value(gross_draw_for_net_cash / item.opening_invested_balance * Decimal("100"))
    return {
        "retirement_deduction_rate": retirement_deduction_rate,
        "capital_tax_rate": capital_tax_rate,
        "retirement_deductions": retirement_deductions,
        "net_retirement_income": net_retirement_income,
        "net_income": net_income,
        "net_cash_gap": net_cash_gap,
        "capital_tax_drag": capital_tax_drag,
        "gross_draw_for_net_cash": gross_draw_for_net_cash,
        "tax_aware_draw_percent": tax_aware_draw_percent,
    }
