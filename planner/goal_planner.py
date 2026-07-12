from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .finance import quantize_money
from .projections import build_projection, first_of_month


@dataclass(frozen=True)
class GoalPlanResult:
    target_net_worth: Decimal
    target_month: date
    monthly_contribution: Decimal
    reached_without_extra: bool
    solvable: bool
    ending_net_worth: Decimal
    gap: Decimal
    iterations: int


def _target_index(projection, target_month):
    target_month = first_of_month(target_month)
    matching = [item for item in projection if item.month <= target_month]
    if not matching:
        return None
    return matching[-1].index


def _net_worth_at_target(household, target_month, stress):
    projection = build_projection(household, stress=stress)
    index = _target_index(projection, target_month)
    if index is None:
        return Decimal("0.00")
    return projection[index].net_worth


def solve_monthly_contribution(
    household,
    *,
    target_net_worth,
    target_month,
    start_month=None,
    target_account=None,
    max_monthly_contribution=Decimal("200000.00"),
    iterations=28,
):
    """Find the additional monthly surplus needed to hit a net-worth target.

    The solver deliberately reuses ``build_projection`` with a projection-only
    stress contribution, so debt, depot growth, taxes, planned purchases, and
    existing rules continue to behave like the normal forecast.
    """
    target_net_worth = quantize_money(target_net_worth)
    target_month = first_of_month(target_month)
    start_month = first_of_month(start_month or household.start_month)
    target_account_id = target_account.id if target_account else None

    def outcome(monthly_contribution):
        return _net_worth_at_target(
            household,
            target_month,
            {
                "goal_monthly_contribution": quantize_money(monthly_contribution),
                "goal_contribution_start": start_month,
                "goal_contribution_end": target_month,
                "goal_target_account_id": target_account_id,
            },
        )

    base_net_worth = outcome(Decimal("0.00"))
    if base_net_worth >= target_net_worth:
        return GoalPlanResult(
            target_net_worth=target_net_worth,
            target_month=target_month,
            monthly_contribution=Decimal("0.00"),
            reached_without_extra=True,
            solvable=True,
            ending_net_worth=quantize_money(base_net_worth),
            gap=Decimal("0.00"),
            iterations=0,
        )

    high_outcome = outcome(max_monthly_contribution)
    if high_outcome < target_net_worth:
        return GoalPlanResult(
            target_net_worth=target_net_worth,
            target_month=target_month,
            monthly_contribution=max_monthly_contribution,
            reached_without_extra=False,
            solvable=False,
            ending_net_worth=quantize_money(high_outcome),
            gap=quantize_money(target_net_worth - high_outcome),
            iterations=1,
        )

    low = Decimal("0.00")
    high = max_monthly_contribution
    result = high_outcome
    for index in range(iterations):
        midpoint = quantize_money((low + high) / Decimal("2"))
        result = outcome(midpoint)
        if result >= target_net_worth:
            high = midpoint
        else:
            low = midpoint

    final_contribution = quantize_money(high)
    final_net_worth = quantize_money(outcome(final_contribution))
    return GoalPlanResult(
        target_net_worth=target_net_worth,
        target_month=target_month,
        monthly_contribution=final_contribution,
        reached_without_extra=False,
        solvable=True,
        ending_net_worth=final_net_worth,
        gap=quantize_money(max(target_net_worth - final_net_worth, Decimal("0.00"))),
        iterations=iterations,
    )
