import random
from decimal import Decimal

from .finance import monthly_rate_from_annual_percent, quantize_money
from .models import AssetAccount
from .projections import build_projection


DEFAULT_PATHS = 100
DEFAULT_VOLATILITY = Decimal("15.00")
DEFAULT_SEED = 42


def weighted_depot_return_rate(household):
    depots = list(household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT))
    total = sum((account.effective_balance for account in depots), Decimal("0.00"))
    if total <= 0:
        return Decimal("0.00")
    weighted = sum((account.effective_balance * account.depot_annual_return_rate for account in depots), Decimal("0.00"))
    return weighted / total


def percentile(values, percent):
    if not values:
        return Decimal("0.00")
    ordered = sorted(values)
    if len(ordered) == 1:
        return quantize_money(ordered[0])
    rank = (Decimal(str(percent)) / Decimal("100")) * Decimal(len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - Decimal(lower)
    return quantize_money(ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction))


def sampled_monthly_return_path(months, annual_mean_rate, annual_volatility, rng):
    path = []
    for _ in range(months):
        annual_return = Decimal(str(rng.gauss(float(annual_mean_rate), float(annual_volatility))))
        annual_return = min(max(annual_return, Decimal("-80.00")), Decimal("80.00"))
        path.append(monthly_rate_from_annual_percent(annual_return))
    return path


def path_succeeds(projection):
    if not projection:
        return False
    never_negative_liquid = all(month.liquid_balance >= 0 for month in projection)
    portfolio_not_depleted = all(month.invested_balance >= 0 for month in projection)
    return never_negative_liquid and portfolio_not_depleted and projection[-1].net_worth >= 0


def build_sequence_risk_summary(
    household,
    path_count=DEFAULT_PATHS,
    annual_volatility=DEFAULT_VOLATILITY,
    seed=DEFAULT_SEED,
):
    months = household.projection_months
    base_return = weighted_depot_return_rate(household)
    rng = random.Random(seed)
    ending_net_worths = []
    ending_depot_values = []
    success_count = 0
    lowest_liquid = Decimal("0.00")

    for _ in range(path_count):
        sampled_rates = sampled_monthly_return_path(months, base_return, annual_volatility, rng)
        projection = build_projection(household, stress={"depot_monthly_return_rates": sampled_rates})
        if path_succeeds(projection):
            success_count += 1
        ending_net_worths.append(projection[-1].net_worth if projection else Decimal("0.00"))
        ending_depot_values.append(projection[-1].invested_balance if projection else Decimal("0.00"))
        if projection:
            path_low = min((month.liquid_balance for month in projection), default=Decimal("0.00"))
            lowest_liquid = min(lowest_liquid, path_low)

    success_probability = (
        quantize_money(Decimal(success_count) / Decimal(path_count) * Decimal("100"))
        if path_count
        else Decimal("0.00")
    )
    return {
        "path_count": path_count,
        "seed": seed,
        "base_annual_return_rate": quantize_money(base_return),
        "annual_volatility": quantize_money(annual_volatility),
        "success_count": success_count,
        "success_probability": success_probability,
        "lowest_liquid": quantize_money(lowest_liquid),
        "ending_net_worth": {
            "p10": percentile(ending_net_worths, 10),
            "p50": percentile(ending_net_worths, 50),
            "p90": percentile(ending_net_worths, 90),
        },
        "ending_depot_value": {
            "p10": percentile(ending_depot_values, 10),
            "p50": percentile(ending_depot_values, 50),
            "p90": percentile(ending_depot_values, 90),
        },
    }
