"""Shared money and finance primitives.

One home for rounding, currency formatting, inflation discounting, and rate
conversion so these conventions cannot drift between modules (the projection,
analytics, retirement, and snapshot layers previously each had their own).
"""

from decimal import Decimal, ROUND_HALF_UP


def quantize_money(value):
    """Round to 2 decimal places, half-up (the convention used everywhere)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_value(value):
    """Format a (possibly None) amount as a 2dp string, half-up."""
    return str(quantize_money(value or Decimal("0.00")))


def inflation_factor(annual_rate, months_from_start):
    if not annual_rate or months_from_start <= 0:
        return Decimal("1.00")
    return (Decimal("1.00") + (annual_rate / Decimal("100"))) ** (Decimal(months_from_start) / Decimal("12"))


def real_value(value, annual_rate, months_from_start):
    """Discount a nominal amount to today's money at the given inflation rate."""
    return value / inflation_factor(annual_rate, months_from_start)


def monthly_rate_from_annual_percent(annual_percent):
    """Compounded monthly rate equivalent to an annual percentage."""
    if not annual_percent:
        return Decimal("0.00")
    annual_rate = annual_percent / Decimal("100")
    return (Decimal("1.00") + annual_rate) ** (Decimal("1.00") / Decimal("12")) - Decimal("1.00")
