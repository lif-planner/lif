from decimal import Decimal


def _bar_width(value, scale):
    if scale <= 0:
        return 0
    return int(min(100, max(4, abs(value) / scale * 100)))


def build_liquidity_view(projection):
    if not projection:
        return {
            "months": [],
            "stress_months": [],
            "lowest_liquid_month": None,
            "lowest_net_worth_month": None,
            "scale": Decimal("0.00"),
        }

    scale = max(
        [abs(item.liquid_balance) for item in projection]
        + [abs(item.net_worth) for item in projection]
        + [Decimal("1.00")]
    )
    months = []
    stress_months = []

    for item in projection:
        cash_stress = item.liquid_balance < 0 <= item.net_worth
        month = {
            "month": item.month,
            "label": item.month.strftime("%b %y"),
            "liquid_balance": item.liquid_balance,
            "lowest_liquid_balance": item.liquid_balance,
            "net_worth": item.net_worth,
            "cash_width": _bar_width(item.liquid_balance, scale),
            "net_worth_width": _bar_width(item.net_worth, scale),
            "cash_stress": cash_stress,
        }
        months.append(month)
        if cash_stress:
            stress_months.append(month)

    return {
        "months": months,
        "stress_months": stress_months,
        "lowest_liquid_month": min(months, key=lambda item: item["liquid_balance"]),
        "lowest_net_worth_month": min(months, key=lambda item: item["net_worth"]),
        "scale": scale,
    }


def build_yearly_liquidity_view(yearly_projection):
    if not yearly_projection:
        return {
            "months": [],
            "stress_months": [],
            "lowest_liquid_month": None,
            "lowest_net_worth_month": None,
            "scale": Decimal("0.00"),
        }

    scale = max(
        [abs(item.ending_liquid_balance) for item in yearly_projection]
        + [abs(item.ending_net_worth) for item in yearly_projection]
        + [Decimal("1.00")]
    )
    months = []
    stress_months = []

    for item in yearly_projection:
        cash_stress = item.stress_months > 0
        year = {
            "month": None,
            "label": item.label,
            "liquid_balance": item.ending_liquid_balance,
            "net_worth": item.ending_net_worth,
            "lowest_liquid_balance": item.lowest_liquid_balance,
            "cash_width": _bar_width(item.ending_liquid_balance, scale),
            "net_worth_width": _bar_width(item.ending_net_worth, scale),
            "cash_stress": cash_stress,
            "stress_months": item.stress_months,
        }
        months.append(year)
        if cash_stress:
            stress_months.append(year)

    return {
        "months": months,
        "stress_months": stress_months,
        "lowest_liquid_month": min(months, key=lambda item: item["lowest_liquid_balance"]),
        "lowest_net_worth_month": min(months, key=lambda item: item["net_worth"]),
        "scale": scale,
    }
