from decimal import Decimal

from .analytics import build_analytics_milestones
from .forecast_explain import forecast_warnings, grouped_audit_lines
from .projections import build_projection, build_yearly_projection
from .quality import build_quality_report


def build_report_year(household, selected_year=None):
    projection = build_projection(household)
    yearly_projection = build_yearly_projection(projection, household.cash_goals.all())
    if not yearly_projection:
        return {
            "projection": projection,
            "yearly_projection": yearly_projection,
            "year_options": [],
            "selected_year": None,
            "year": None,
            "months": [],
            "groups": [],
            "warnings": [],
            "quality_issues": [],
            "depot_activity": [],
            "debt_activity": [],
            "assumptions": _assumptions(household),
            "summary": {},
            "journey": {},
        }

    years = [item.year for item in yearly_projection]
    if selected_year not in years:
        selected_year = years[0]
    year = next(item for item in yearly_projection if item.year == selected_year)
    months = projection[year.start_index : year.end_index + 1]
    quality_report = build_quality_report(household)
    return {
        "projection": projection,
        "yearly_projection": yearly_projection,
        "year_options": [{"value": item.year, "label": item.label} for item in yearly_projection],
        "selected_year": selected_year,
        "year": year,
        "months": months,
        "groups": grouped_audit_lines(year.audit_lines),
        "warnings": forecast_warnings(year, household, months),
        "quality_issues": quality_report["issues"][:6],
        "depot_activity": _activity_rows(
            year.audit_lines,
            {
                "Depot growth",
                "Depot payout",
                "Depot draw",
                "Investment purchase",
                "Planned investment purchase",
                "Planned investment payout",
            },
        ),
        "debt_activity": _activity_rows(
            year.audit_lines,
            {
                "Debt",
                "Extra repayment",
                "Private loan disbursed",
                "Private loan gift",
                "Private loan interest",
                "Private loan principal",
            },
        ),
        "assumptions": _assumptions(household),
        "summary": _summary(year),
        "journey": _journey(household, projection, yearly_projection),
    }


def _activity_rows(audit_lines, sections):
    return [line for line in audit_lines if line.section in sections]


def _assumptions(household):
    return [
        ("Planning start", household.start_month.strftime("%Y-%m")),
        (
            "Planning horizon",
            f"{household.planning_years} years" if household.planning_years else f"{household.planning_months} months",
        ),
        ("Inflation", f"{household.annual_inflation_rate}%"),
        ("Default currency", household.currency),
        ("Emergency fund", f"{household.emergency_fund_months} months"),
    ]


def _summary(year):
    income_gap = year.income - year.annual_cash_goal
    if year.annual_cash_goal <= 0:
        fire_status = "No cash goal configured"
    elif income_gap >= 0:
        fire_status = "Income covers cash goal"
    elif year.portfolio_draw_percent <= Decimal("4.00"):
        fire_status = "Portfolio-supported"
    else:
        fire_status = "Needs portfolio draw above 4%"
    return {
        "income_gap": income_gap,
        "fire_status": fire_status,
    }


def _journey(household, projection, yearly_projection):
    start_month = projection[0] if projection else None
    start_year = yearly_projection[0].year if yearly_projection else None
    next_year = _year_at_or_after(yearly_projection, start_year + 1) if start_year else None
    five_year = _year_at_or_after(yearly_projection, start_year + 5) if start_year else None
    retirement_year = next((item for item in yearly_projection if item.retirement_income > 0), None)
    final_year = yearly_projection[-1] if yearly_projection else None
    major_events = _major_events(household)[:8]
    return {
        "start_month": start_month,
        "next_year": next_year or final_year,
        "five_year": five_year or final_year,
        "retirement_year": retirement_year,
        "final_year": final_year,
        "major_events": major_events,
        "chart_points": _journey_chart_points(yearly_projection, retirement_year),
        "retirement_points": _retirement_chart_points(yearly_projection),
        "headline": _journey_headline(yearly_projection),
    }


def _year_at_or_after(yearly_projection, target_year):
    return next((item for item in yearly_projection if item.year >= target_year), None)


def _journey_headline(yearly_projection):
    if not yearly_projection:
        return "No projection yet"
    first_sustainable = next(
        (
            item
            for item in yearly_projection
            if item.annual_cash_goal > 0
            and (item.cash_goal_gap <= 0 or (item.opening_invested_balance > 0 and item.portfolio_draw_percent <= Decimal("4.00")))
        ),
        None,
    )
    if first_sustainable:
        return f"Plan becomes self-sustaining around {first_sustainable.label}"
    lowest = min(yearly_projection, key=lambda item: item.lowest_liquid_balance)
    if lowest.lowest_liquid_balance < 0:
        return f"Cash gets tight around {lowest.label}"
    return f"Plan stays liquid through {yearly_projection[-1].label}"


def _major_events(household):
    relevant = []
    priority = {
        "Children": 0,
        "Retirement": 1,
        "Debt": 2,
        "Depot": 3,
        "Income": 4,
        "Equity": 5,
        "Cash Goal": 6,
    }
    for item in build_analytics_milestones(household):
        if item["category"] not in priority:
            continue
        relevant.append({**item, "priority": priority[item["category"]]})
    return sorted(relevant, key=lambda item: (item["date"], item["priority"], item["label"]))


def _selected_years(yearly_projection, retirement_year):
    if not yearly_projection:
        return []
    selected = [yearly_projection[0]]
    if len(yearly_projection) > 1:
        selected.append(yearly_projection[min(1, len(yearly_projection) - 1)])
    if len(yearly_projection) > 5:
        selected.append(yearly_projection[5])
    if retirement_year:
        selected.append(retirement_year)
    selected.append(yearly_projection[-1])
    by_year = {}
    for item in selected:
        by_year[item.year] = item
    return list(by_year.values())


def _bar_width(value, max_value):
    if max_value <= 0:
        return "0"
    return f"{(abs(value) / max_value * Decimal('100')).quantize(Decimal('0.1'))}"


def _journey_chart_points(yearly_projection, retirement_year):
    selected = _selected_years(yearly_projection, retirement_year)
    max_value = max(
        (abs(item.ending_net_worth) for item in selected),
        default=Decimal("0.00"),
    )
    return [
        {
            "label": item.label,
            "liquid": item.ending_liquid_balance,
            "invested": item.ending_invested_balance,
            "liabilities": item.ending_liability_balance,
            "net_worth": item.ending_net_worth,
            "net_worth_width": _bar_width(item.ending_net_worth, max_value),
            "liquid_width": _bar_width(item.ending_liquid_balance, max_value),
            "invested_width": _bar_width(item.ending_invested_balance, max_value),
            "liability_width": _bar_width(item.ending_liability_balance, max_value),
        }
        for item in selected
    ]


def _retirement_chart_points(yearly_projection):
    retirement_years = [item for item in yearly_projection if item.retirement_income > 0]
    if not retirement_years:
        return []
    sample = retirement_years[:8]
    max_value = max(
        (
            max(item.annual_cash_goal, item.income, item.cash_goal_gap)
            for item in sample
        ),
        default=Decimal("0.00"),
    )
    return [
        {
            "label": item.label,
            "income": item.income,
            "cash_goal": item.annual_cash_goal,
            "gap": item.cash_goal_gap,
            "draw_percent": item.portfolio_draw_percent,
            "income_width": _bar_width(item.income, max_value),
            "goal_width": _bar_width(item.annual_cash_goal, max_value),
            "gap_width": _bar_width(item.cash_goal_gap, max_value),
        }
        for item in sample
    ]
