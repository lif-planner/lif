"""Import & readiness domain: import-batch decoration/detail, pre-import
snapshots, the household readiness checklist, and the import runbook /
reconciliation. Extracted from the view layer to keep it testable."""

from django.urls import reverse
from django.utils import timezone

from .feature_flags import feature_flag_map
from .models import (
    AssetAccount,
    BackupEvent,
    DepotHolding,
    Household,
    ImportBatch,
    MoneyRule,
    Person,
    Snapshot,
)
from .moneymoney_service import build_moneymoney_mapping_review
from .quality import build_quality_report
from .snapshots import build_snapshot_summary, compare_snapshot_to_current


def latest_import_batch(household, *import_kinds):
    return household.import_batches.filter(summary__import_kind__in=import_kinds).first()


def checklist_item(key, label, complete, detail, action_url="", action_label=""):
    return {
        "key": key,
        "label": label,
        "complete": complete,
        "detail": detail,
        "action_url": action_url,
        "action_label": action_label,
    }


def build_import_runbook(household, moneymoney_diagnostics=None):
    flags = feature_flag_map()
    quality_report = build_quality_report(household)
    people_count = household.people.count()
    income_count = household.rules.filter(kind=MoneyRule.Kind.INCOME, is_active=True).count()
    cash_goal_count = household.cash_goals.filter(is_active=True).count()
    mapping_review = build_moneymoney_mapping_review(household)
    mapping_count = mapping_review["mapping_count"]
    latest_account_batch = latest_import_batch(household, "accounts", "moneymoney_accounts")
    latest_holding_batch = latest_import_batch(household, "depot_holdings", "moneymoney_depot_holdings")
    applied_account_batch = bool(latest_account_batch and latest_account_batch.status == ImportBatch.Status.APPLIED)
    applied_holding_batch = bool(latest_holding_batch and latest_holding_batch.status == ImportBatch.Status.APPLIED)
    diagnostics_complete = bool(moneymoney_diagnostics and moneymoney_diagnostics.get("reachable"))

    items = [
        checklist_item(
            "setup",
            "Create household foundation",
            people_count > 0 and income_count > 0 and cash_goal_count > 0,
            f"{people_count} people, {income_count} active income rules, {cash_goal_count} active cash goals.",
            reverse("planner:setup"),
            "Open setup",
        ),
        checklist_item(
            "money_flag",
            "Enable MoneyMoney import",
            flags.get("moneymoney_import", False),
            "Feature flag moneymoney_import controls the live local connector buttons.",
            "/admin/planner/featureflag/",
            "Open flags",
        ),
        checklist_item(
            "diagnostics",
            "Run MoneyMoney diagnostics",
            diagnostics_complete,
            "Use diagnostics before importing to check py-money, MoneyMoney reachability, and counts.",
            reverse("planner:import_center"),
            "Open imports",
        ),
        checklist_item(
            "mappings",
            "Review account type overrides",
            mapping_review["review_complete"],
            (
                f"{mapping_count} override(s), {mapping_review['needs_review_count']} defaulted cash account(s) need review."
                if mapping_review["latest_preview"]
                else "Run a MoneyMoney account preview so account names can be reviewed."
            ),
            reverse("planner:moneymoney_mappings"),
            "Edit mappings",
        ),
        checklist_item(
            "accounts",
            "Preview and apply accounts",
            applied_account_batch,
            batch_detail(latest_account_batch, "No account import batch yet."),
            reverse("planner:import_center"),
            "Open imports",
        ),
        checklist_item(
            "holdings",
            "Preview and apply depot holdings",
            applied_holding_batch,
            batch_detail(latest_holding_batch, "No depot holding import batch yet."),
            reverse("planner:import_center"),
            "Open imports",
        ),
        checklist_item(
            "quality",
            "Review data quality",
            quality_report["counts"]["critical"] == 0,
            f"{quality_report['counts']['critical']} critical, {quality_report['counts']['warning']} warning, {quality_report['counts']['info']} info issue(s).",
            reverse("planner:data_quality"),
            "Open quality",
        ),
        checklist_item(
            "analytics",
            "Check analytics",
            applied_account_batch and quality_report["counts"]["critical"] == 0,
            "Review projection charts after the foundation and imports are in place.",
            reverse("planner:analytics"),
            "Open analytics",
        ),
    ]
    next_item = next((item for item in items if not item["complete"]), None)
    return {
        "items": items,
        "next_item": next_item,
        "quality_report": quality_report,
        "latest_account_batch": latest_account_batch,
        "latest_holding_batch": latest_holding_batch,
        "mapping_count": mapping_count,
        "moneymoney_diagnostics": moneymoney_diagnostics,
    }


def readiness_summary(items):
    complete = sum(1 for item in items if item["complete"])
    total = len(items)
    return {
        "complete": complete,
        "total": total,
        "percent": round((complete / total) * 100) if total else 100,
        "next_item": next((item for item in items if not item["complete"]), None),
    }


def build_household_readiness(household, moneymoney_diagnostics=None):
    import_runbook = build_import_runbook(household, moneymoney_diagnostics)
    quality_report = import_runbook["quality_report"]
    adults_count = household.people.filter(role=Person.Role.ADULT).count()
    children_count = household.people.filter(role=Person.Role.CHILD).count()
    income_count = household.rules.filter(kind=MoneyRule.Kind.INCOME, is_active=True).count()
    expense_count = household.rules.filter(kind=MoneyRule.Kind.EXPENSE, is_active=True).count()
    active_cash_goals = household.cash_goals.filter(is_active=True)
    depot_count = household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT).count()
    holdings_count = DepotHolding.objects.filter(asset_account__household=household).count()
    debt_count = household.debts.filter(is_active=True).count()
    retirement_count = household.retirement_plans.filter(is_active=True).count()
    applied_import_count = household.import_batches.filter(status=ImportBatch.Status.APPLIED).count()
    backup_count = BackupEvent.objects.filter(action=BackupEvent.Action.BACKUP, status=BackupEvent.Status.SUCCEEDED).count()
    reconciliation = build_import_reconciliation(household)
    integrity = quality_report.get("integrity", {"ok": False, "checked": 0, "failure_count": 0})

    data_mode_items = [
        checklist_item(
            "real_mode",
            "Mark household as real data",
            household.data_mode == Household.DataMode.REAL,
            "The top bar should clearly distinguish demo exploration from private real planning data.",
            reverse("planner:real_data_readiness"),
            "Open readiness",
        ),
        checklist_item(
            "real_name",
            "Rename the demo household",
            not household.name.startswith("Demo:"),
            f"Current household name: {household.name}.",
            reverse("planner:household_settings"),
            "Edit household",
        ),
        checklist_item(
            "backup",
            "Create at least one local backup",
            backup_count > 0,
            f"{backup_count} successful backup event(s) recorded.",
            reverse("planner:backup_center"),
            "Open backups",
        ),
    ]

    completeness_items = [
        checklist_item(
            "adults",
            "Add contributing adults",
            adults_count > 0,
            f"{adults_count} adult(s) configured.",
            reverse("planner:setup"),
            "Open setup",
        ),
        checklist_item(
            "income",
            "Add recurring income",
            income_count > 0,
            f"{income_count} active income rule(s).",
            reverse("planner:rule_create"),
            "Add rule",
        ),
        checklist_item(
            "expenses",
            "Add recurring costs",
            expense_count > 0,
            f"{expense_count} active expense rule(s).",
            reverse("planner:rule_create"),
            "Add rule",
        ),
        checklist_item(
            "cash_goals",
            "Set yearly cash goals",
            active_cash_goals.exists(),
            f"{active_cash_goals.count()} active yearly cash goal(s).",
            reverse("planner:cash_goal_create"),
            "Add cash goal",
        ),
        checklist_item(
            "accounts",
            "Add current accounts",
            household.accounts.exists(),
            f"{household.accounts.count()} account(s) configured.",
            reverse("planner:account_setup"),
            "Account wizard",
        ),
        checklist_item(
            "depot_holdings",
            "Break depot into holdings",
            depot_count == 0 or holdings_count > 0,
            f"{depot_count} depot account(s), {holdings_count} holding(s).",
            reverse("planner:depot_holding_create"),
            "Add holding",
        ),
        checklist_item(
            "mortgages",
            "Model debts and mortgages",
            household.accounts.filter(account_type=AssetAccount.AccountType.LOAN).count() == 0 or debt_count > 0,
            f"{debt_count} active debt repayment model(s).",
            reverse("planner:debt_create"),
            "Add debt",
        ),
        checklist_item(
            "retirement",
            "Add retirement assumptions",
            adults_count == 0 or retirement_count > 0,
            f"{retirement_count} active retirement plan(s).",
            reverse("planner:retirement_plan_create"),
            "Add pension",
        ),
        checklist_item(
            "quality",
            "Resolve critical quality issues",
            quality_report["counts"]["critical"] == 0,
            f"{quality_report['counts']['critical']} critical, {quality_report['counts']['warning']} warning, {quality_report['counts']['info']} info issue(s).",
            reverse("planner:data_quality"),
            "Open quality",
        ),
    ]

    reconciliation_items = [
        checklist_item(
            "preview_import",
            "Dry-run imports before applying",
            household.import_batches.exists(),
            f"{household.import_batches.count()} import batch(es) recorded.",
            reverse("planner:import_center"),
            "Open imports",
        ),
        checklist_item(
            "applied_import",
            "Apply clean foundation imports",
            applied_import_count > 0,
            f"{applied_import_count} applied import batch(es).",
            reverse("planner:import_center"),
            "Open imports",
        ),
        checklist_item(
            "runbook",
            "Complete import runbook",
            import_runbook["next_item"] is None,
            "The runbook checks local connector readiness, mappings, applied imports, quality, and analytics review.",
            reverse("planner:import_runbook"),
            "Open runbook",
        ),
    ]

    trust_items = [
        checklist_item(
            "projection_integrity",
            "Verify projection integrity",
            integrity["ok"] and integrity["checked"] > 0,
            f"{integrity['checked']} reconciliation check(s), {integrity['failure_count']} failure(s).",
            reverse("planner:projection_integrity"),
            "Open integrity",
        ),
        checklist_item(
            "starting_balances",
            "Reconcile starting balances",
            reconciliation["next_item"] is None,
            (
                reconciliation["next_item"]["detail"]
                if reconciliation["next_item"]
                else "No import reconciliation action is currently queued."
            ),
            reverse("planner:reconciliation_center"),
            "Open reconciliation",
        ),
        checklist_item(
            "assumptions",
            "Review long-range assumptions",
            quality_report["counts"]["critical"] == 0 and household.cash_goals.filter(is_active=True).exists(),
            "Check inflation, depot returns, taxes, cash goals, debt rates, pensions, and draw rules before using retirement results.",
            reverse("planner:assumptions_registry"),
            "Open assumptions",
        ),
        checklist_item(
            "scenario_comparison",
            "Compare major what-if scenarios",
            household.scenarios.filter(is_active=True).exists()
            or household.family_gift_plans.filter(is_active=True).exists()
            or household.real_estate_transfer_plans.filter(is_active=True).exists(),
            "Review at least one scenario, estate comparison, or stress preset before treating the base plan as a decision.",
            reverse("planner:scenario_compare"),
            "Open scenarios",
        ),
    ]

    sections = [
        {"key": "mode", "title": "Demo vs Real Data", "items": data_mode_items, **readiness_summary(data_mode_items)},
        {"key": "completeness", "title": "Data Completeness", "items": completeness_items, **readiness_summary(completeness_items)},
        {"key": "reconciliation", "title": "Import and Reconciliation", "items": reconciliation_items, **readiness_summary(reconciliation_items)},
        {"key": "trust", "title": "Before You Trust the Forecast", "items": trust_items, **readiness_summary(trust_items)},
    ]
    all_items = [item for section in sections for item in section["items"]]
    summary = readiness_summary(all_items)
    return {
        "sections": sections,
        "summary": summary,
        "quality_report": quality_report,
        "import_runbook": import_runbook,
        "trust_items": trust_items,
    }


def batch_detail(batch, empty_detail):
    if not batch:
        return empty_detail
    return f"Latest batch #{batch.pk}: {batch.get_status_display()}, {batch.valid_count} valid row(s), {batch.error_count} error(s)."


def batch_warning_count(batch):
    return (batch.summary or {}).get("warning_count", 0)


def batch_kind_label(batch):
    import_kind = (batch.summary or {}).get("import_kind", "")
    return {
        "accounts": "Accounts",
        "depot_holdings": "Depot holdings",
        "moneymoney_accounts": "MoneyMoney accounts",
        "moneymoney_depot_holdings": "MoneyMoney depot holdings",
    }.get(import_kind, batch.get_source_display())


def decorate_import_batch(batch):
    batch.warning_count = batch_warning_count(batch)
    batch.kind_label = batch_kind_label(batch)
    batch.can_apply = batch.status == ImportBatch.Status.DRY_RUN and batch.error_count == 0 and batch.row_count > 0
    batch.missing_columns = (batch.summary or {}).get("missing_columns", [])
    batch.import_kind = (batch.summary or {}).get("import_kind", "")
    batch.is_holding_import = batch.import_kind in {"depot_holdings", "moneymoney_depot_holdings"}
    if batch.status == ImportBatch.Status.FAILED or batch.error_count:
        batch.status_severity = "critical"
    elif batch.status == ImportBatch.Status.DRY_RUN:
        batch.status_severity = "warning"
    else:
        batch.status_severity = "ok"
    return batch


def build_import_batch_detail(batch):
    batch = decorate_import_batch(batch)
    rows = (batch.summary or {}).get("rows", [])
    apply_result = (batch.summary or {}).get("apply_result", {})
    pre_apply_snapshot = None
    import_comparison = None
    snapshot_id = apply_result.get("pre_apply_snapshot_id")
    if snapshot_id:
        pre_apply_snapshot = Snapshot.objects.filter(pk=snapshot_id, household=batch.household).first()
        if pre_apply_snapshot:
            import_comparison = compare_snapshot_to_current(
                pre_apply_snapshot.summary,
                build_snapshot_summary(batch.household),
            )
    changed_rows = [row for row in rows if row.get("changes")]
    warning_rows = [row for row in rows if row.get("warnings")]
    error_rows = [row for row in rows if row.get("errors")]
    unchanged_rows = [row for row in rows if row.get("action") == "unchanged"]
    blocked_reason = ""
    if batch.status == ImportBatch.Status.APPLIED:
        blocked_reason = "This batch has already been applied."
    elif batch.missing_columns:
        blocked_reason = "This batch is missing required columns."
    elif batch.error_count:
        blocked_reason = "This batch has validation errors."
    elif batch.row_count == 0:
        blocked_reason = "This batch has no rows to apply."
    return {
        "batch": batch,
        "rows": rows,
        "apply_result": apply_result,
        "pre_apply_snapshot": pre_apply_snapshot,
        "import_comparison": import_comparison,
        "default_create_snapshot": batch.household.data_mode == Household.DataMode.REAL,
        "changed_rows": changed_rows,
        "warning_rows": warning_rows,
        "error_rows": error_rows,
        "unchanged_rows": unchanged_rows,
        "blocked_reason": blocked_reason,
    }


def create_pre_import_snapshot(household, batch):
    today = timezone.localdate()
    return Snapshot.objects.create(
        household=household,
        name=f"Before import batch #{batch.pk}",
        snapshot_type=Snapshot.SnapshotType.PRE_IMPORT,
        snapshot_date=today,
        summary=build_snapshot_summary(household),
        notes=f"Created before applying import batch #{batch.pk} ({batch_kind_label(batch)}).",
    )


def build_import_reconciliation(household):
    batches = [decorate_import_batch(batch) for batch in household.import_batches.all()[:25]]
    clean_pending_batches = [
        batch
        for batch in batches
        if batch.status == ImportBatch.Status.DRY_RUN and batch.error_count == 0 and batch.row_count > 0
    ]
    warning_pending_batches = [batch for batch in clean_pending_batches if batch.warning_count]
    clean_pending_batches = [batch for batch in clean_pending_batches if not batch.warning_count]
    blocked_batches = [
        batch
        for batch in batches
        if batch.status == ImportBatch.Status.FAILED
        or batch.error_count > 0
        or bool((batch.summary or {}).get("missing_columns"))
    ]
    latest_applied_account_batch = (
        household.import_batches.filter(
            status=ImportBatch.Status.APPLIED,
            summary__import_kind__in=["accounts", "moneymoney_accounts"],
        )
        .order_by("-created_at")
        .first()
    )
    latest_applied_holding_batch = (
        household.import_batches.filter(
            status=ImportBatch.Status.APPLIED,
            summary__import_kind__in=["depot_holdings", "moneymoney_depot_holdings"],
        )
        .order_by("-created_at")
        .first()
    )
    today = timezone.localdate()
    accounts = list(household.accounts.prefetch_related("holdings"))
    stale_accounts = [
        account
        for account in accounts
        if account.as_of_date and (today - account.as_of_date).days > 45
    ]
    depot_differences = [
        {
            "account": account,
            "difference": account.depot_difference,
        }
        for account in accounts
        if account.account_type == AssetAccount.AccountType.DEPOT
        and account.holdings.exists()
        and account.depot_difference
    ]
    next_item = None
    if blocked_batches:
        next_item = {
            "label": "Fix blocked imports",
            "detail": f"{len(blocked_batches)} recent batch(es) have errors or missing columns.",
            "severity": "critical",
        }
    elif warning_pending_batches:
        next_item = {
            "label": "Review import warnings",
            "detail": f"{len(warning_pending_batches)} valid dry-run batch(es) have duplicate or matching warnings. Review rows before applying.",
            "severity": "warning",
        }
    elif clean_pending_batches:
        next_item = {
            "label": "Apply or discard clean dry-runs",
            "detail": f"{len(clean_pending_batches)} clean dry-run batch(es) are waiting.",
            "severity": "warning",
        }
    elif stale_accounts:
        next_item = {
            "label": "Refresh stale account valuations",
            "detail": f"{len(stale_accounts)} account valuation date(s) are older than 45 days.",
            "severity": "warning",
        }
    elif depot_differences:
        next_item = {
            "label": "Reconcile depot totals",
            "detail": f"{len(depot_differences)} depot account(s) differ from summed holdings.",
            "severity": "warning",
        }

    return {
        "clean_pending_batches": clean_pending_batches,
        "warning_pending_batches": warning_pending_batches,
        "blocked_batches": blocked_batches,
        "latest_applied_account_batch": latest_applied_account_batch,
        "latest_applied_holding_batch": latest_applied_holding_batch,
        "stale_accounts": stale_accounts,
        "depot_differences": depot_differences,
        "next_item": next_item,
    }
