from datetime import date

from .feature_flags import feature_flag_map
from .privacy import PRIVACY_MODE_SESSION_KEY
from lif.version import version_context


PRIORITY_ORDER = {"critical": 0, "review": 1, "optional": 2}


def attention_item(
    key,
    label,
    detail,
    url="",
    action_label="Open",
    severity="warning",
    priority="review",
    dismissible=True,
):
    return {
        "key": key,
        "label": label,
        "detail": detail,
        "url": url,
        "action_label": action_label,
        "severity": severity,
        "priority": priority,
        "dismissible": dismissible,
    }


def hidden_item_active(hidden_value):
    hidden_until = hidden_value.get("hidden_until") if isinstance(hidden_value, dict) else ""
    if not hidden_until:
        return True
    try:
        return date.fromisoformat(hidden_until) >= date.today()
    except ValueError:
        return True


def visible_attention_items(items, hidden):
    hidden = hidden or {}
    return [
        item
        for item in items
        if item["key"] not in hidden or not hidden_item_active(hidden[item["key"]])
    ]


def dedupe_attention_items(items):
    seen_keys = set()
    seen_labels = set()
    deduped = []
    for item in items:
        label_key = item["label"].strip().lower()
        if item["key"] in seen_keys or label_key in seen_labels:
            continue
        seen_keys.add(item["key"])
        seen_labels.add(label_key)
        deduped.append(item)
    return deduped


def sort_attention_items(items):
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(
        items,
        key=lambda item: (
            PRIORITY_ORDER.get(item["priority"], 9),
            severity_order.get(item["severity"], 9),
            item["label"],
        ),
    )


def build_attention_items(household, flags, include_hidden=False):
    from django.urls import reverse

    from .assumptions import build_assumption_registry
    from .models import AssetAccount, ImportBatch, MoneyRule, Person, SnapshotReviewAction

    items = []
    if household.data_mode == household.DataMode.DEMO:
        items.append(
            attention_item(
                "demo_data",
                "Demo data",
                "Replace sample values before using private planning numbers.",
                reverse("planner:onboarding"),
                "Onboarding",
            )
        )

    if not household.accounts.exists():
        items.append(
            attention_item(
                "no_accounts",
                "No accounts configured",
                "Add at least one cash, savings, depot, or loan account before relying on projections.",
                reverse("planner:account_create"),
                "Add account",
                severity="critical",
                priority="critical",
                dismissible=False,
            )
        )
    elif not household.accounts.filter(
        account_type__in=[
            AssetAccount.AccountType.CASH,
            AssetAccount.AccountType.SAVINGS,
        ]
    ).exists():
        items.append(
            attention_item(
                "no_liquid_account",
                "No liquid account",
                "Liquidity checks need at least one cash or savings account.",
                reverse("planner:account_create"),
                "Add account",
                severity="critical",
                priority="critical",
                dismissible=False,
            )
        )

    people_count = household.people.count()
    income_count = household.rules.filter(kind=MoneyRule.Kind.INCOME, is_active=True).count()
    cash_goal_count = household.cash_goals.filter(is_active=True).count()
    if people_count == 0 or income_count == 0 or cash_goal_count == 0:
        items.append(
            attention_item(
                "finish_foundation",
                "Finish foundation",
                f"{people_count} people, {income_count} income rules, {cash_goal_count} cash goals.",
                reverse("planner:onboarding"),
                "Continue",
            )
        )

    if flags.get("imports") and not household.import_batches.filter(status=ImportBatch.Status.APPLIED).exists():
        items.append(
            attention_item(
                "no_applied_import",
                "No applied import",
                "Preview and apply local account data when ready.",
                reverse("planner:import_center"),
                "Imports",
                severity="info",
                priority="optional",
            )
        )

    open_action = SnapshotReviewAction.objects.filter(
        review__household=household,
        status=SnapshotReviewAction.Status.OPEN,
    ).select_related("review").first()
    if open_action:
        items.append(
            attention_item(
                f"review_action_{open_action.pk}",
                open_action.title,
                f"Open annual review action from {open_action.review.title}.",
                reverse("planner:snapshot_review"),
                "Review",
            )
        )

    adults_count = household.people.filter(role=Person.Role.ADULT).count()
    retirement_count = household.retirement_plans.filter(is_active=True).count()
    if adults_count and not retirement_count:
        items.append(
            attention_item(
                "no_retirement_plans",
                "No retirement plans",
                "Add pension assumptions for contributing adults.",
                reverse("planner:retirement_plan_create"),
                "Add pension",
                severity="info",
                priority="optional",
            )
        )

    if household.accounts.filter(
        account_type=AssetAccount.AccountType.DEPOT,
        depot_valuation=AssetAccount.DepotValuation.HOLDINGS_SUM,
        holdings__isnull=True,
    ).exists():
        items.append(
            attention_item(
                "depot_holdings_missing",
                "Depot has no holdings",
                "A depot using holdings valuation needs at least one holding.",
                reverse("planner:holding_index"),
                "Review depot",
                severity="critical",
                priority="critical",
                dismissible=False,
            )
        )

    if household.debts.filter(is_active=True, fixed_interest_until__isnull=False, refinance_annual_interest_rate__isnull=True).exists():
        items.append(
            attention_item(
                "debt_refinance_missing",
                "Debt refinance assumptions missing",
                "At least one fixed-interest debt has no refinance rate.",
                reverse("planner:debt_index"),
                "Review debts",
            )
        )

    assumption_registry = build_assumption_registry(household, reviews=list(household.assumption_reviews.all()))
    expired_count = assumption_registry["confidence_counts"].get("expired", 0)
    if expired_count:
        items.append(
            attention_item(
                "expired_assumption_reviews",
                "Assumption reviews expired",
                f"{expired_count} planning assumption review(s) are older than the review window.",
                reverse("planner:assumptions_registry"),
                "Review assumptions",
                severity="warning",
                priority="review",
            )
        )

    items = sort_attention_items(dedupe_attention_items(items))
    if not include_hidden:
        items = visible_attention_items(items, household.hidden_attention_items)
    return items[:5]


def feature_flags(request):
    from django.db import OperationalError, ProgrammingError

    from .models import Household
    from .households import active_household

    try:
        current_household = active_household(create=False)
        households = list(Household.objects.order_by("-is_active", "name", "pk"))
    except (OperationalError, ProgrammingError):
        current_household = None
        households = []
    flags = feature_flag_map()
    attention_items = []
    all_attention_items = []
    hidden_attention_count = 0
    if current_household:
        try:
            all_attention_items = build_attention_items(current_household, flags, include_hidden=True)
            attention_items = visible_attention_items(all_attention_items, current_household.hidden_attention_items)
            visible_keys = {item["key"] for item in attention_items}
            hidden_attention_count = len([item for item in all_attention_items if item["key"] not in visible_keys])
        except (OperationalError, ProgrammingError):
            attention_items = []

    return {
        "feature_flags": flags,
        "current_household": current_household,
        "household_switcher": households,
        "attention_items": attention_items,
        "attention_count": len(attention_items),
        "hidden_attention_count": hidden_attention_count,
        "privacy_mode_enabled": request.session.get(PRIVACY_MODE_SESSION_KEY, False),
        **version_context(),
    }
