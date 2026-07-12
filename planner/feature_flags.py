import os
from functools import wraps

from django.db import OperationalError, ProgrammingError
from django.http import Http404


FEATURE_FLAG_DEFINITIONS = {
    "analytics": {
        "default": True,
        "description": "Interactive analytics page with charting and visible projection data.",
    },
    "cash_goals": {
        "default": True,
        "description": "Yearly cash goal planning and FIRE-style ETF draw need calculations.",
    },
    "depot_holdings": {
        "default": True,
        "description": "Depot holding detail tracking, holdings-sum valuation, payout dates, and bond/ETF metadata.",
    },
    "debts": {
        "default": True,
        "description": "Debt and mortgage repayment modeling with fixed-interest and refinance assumptions.",
    },
    "real_estate": {
        "default": True,
        "description": "Residence and investment-property tracking with appreciation, carrying costs, mortgages, and sale events.",
    },
    "income_investments": {
        "default": True,
        "description": "Dated income investments such as solar investments with monthly income.",
    },
    "retirement_plans": {
        "default": True,
        "description": "German statutory pension and private pension income modeling.",
    },
    "equity_grants": {
        "default": True,
        "description": "RSU and other equity grant vesting income modeling.",
    },
    "scenarios": {
        "default": True,
        "description": "Scenario cards and scenario-specific projection adjustments.",
    },
    "true_expenses": {
        "default": True,
        "description": "Irregular true expenses with yearly, quarterly, and monthly timing.",
    },
    "child_milestones": {
        "default": True,
        "description": "Child timeline milestones that adjust household costs or income.",
    },
    "salary_changes": {
        "default": True,
        "description": "Future salary change planning for household members.",
    },
    "imports": {
        "default": True,
        "description": "Import center for CSV, MoneyMoney, and YNAB import workflows.",
    },
    "read_only_mode": {
        "default": False,
        "description": "Temporarily block planner data changes while still allowing read access.",
    },
    "snapshots": {
        "default": False,
        "description": "Freeze planning snapshots and compare them against later reality.",
    },
    "moneymoney_import": {
        "default": False,
        "description": "Import accounts, depot holdings, and transactions from MoneyMoney.",
    },
    "ynab_import": {
        "default": False,
        "description": "Import account and budget foundation data from YNAB.",
    },
    "multi_language": {
        "default": False,
        "description": "Enable in-progress localization features, with German as the first target language.",
    },
    "advanced_tax_model": {
        "default": False,
        "description": "Enable richer German tax modeling beyond simple configured tax rates.",
    },
    "docker_deployment": {
        "default": False,
        "description": "Enable deployment helpers while the dockerized setup is being developed.",
    },
    "mobile_read_only": {
        "default": False,
        "description": "Enable early read-only mobile/iPhone views for charts and projections.",
    },
    "mcp_server": {
        "default": False,
        "description": "Expose a local read-only Model Context Protocol server so an external LLM can inspect inputs, assumptions, and the computed projection to check for inconsistencies.",
    },
}

FEATURE_FLAG_CHOICES = [(key, key.replace("_", " ").title()) for key in FEATURE_FLAG_DEFINITIONS]

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def default_enabled(key):
    definition = FEATURE_FLAG_DEFINITIONS.get(key, {})
    return bool(definition.get("default", False))


def environment_override(key):
    value = os.environ.get(f"LIF_FEATURE_{key.upper()}")
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def feature_enabled(key):
    override = environment_override(key)
    if override is not None:
        return override

    from .models import FeatureFlag

    try:
        flag = FeatureFlag.objects.filter(key=key).only("enabled").first()
    except (OperationalError, ProgrammingError):
        return default_enabled(key)
    if flag is None:
        return default_enabled(key)
    return flag.enabled


def feature_flag_map():
    from .models import FeatureFlag

    values = {key: default_enabled(key) for key in FEATURE_FLAG_DEFINITIONS}
    try:
        for flag in FeatureFlag.objects.filter(key__in=FEATURE_FLAG_DEFINITIONS).only("key", "enabled"):
            values[flag.key] = flag.enabled
    except (OperationalError, ProgrammingError):
        pass

    for key in FEATURE_FLAG_DEFINITIONS:
        override = environment_override(key)
        if override is not None:
            values[key] = override
    return values


def feature_required(key):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not feature_enabled(key):
                raise Http404("Feature is not enabled.")
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
