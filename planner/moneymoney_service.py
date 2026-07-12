"""MoneyMoney import orchestration: discovery, override resolution, and the
mapping-review domain. Kept out of the view layer so it is importable and
testable on its own."""

from .import_adapters.moneymoney import MoneyMoneyConnector, MoneyMoneyConnectorUnavailable
from .models import AssetAccount, MoneyMoneyAccountMapping


def moneymoney_account_type_overrides(household):
    overrides = {}
    for mapping in household.moneymoney_account_mappings.all():
        if mapping.import_enabled and mapping.account_type:
            overrides[mapping.source_key] = mapping.account_type
            if mapping.source_key.startswith("legacy-name:"):
                overrides[mapping.account_name] = mapping.account_type
    return overrides

def disabled_moneymoney_source_keys(household, source_kind=None):
    mappings = household.moneymoney_account_mappings.filter(import_enabled=False)
    if source_kind:
        mappings = mappings.filter(source_kind=source_kind)
    return {mapping.source_key for mapping in mappings if mapping.source_key}

def sync_moneymoney_mapping_rows(household, account_rows):
    synced_count = 0
    created_count = 0
    for row in account_rows:
        name = (row.name or "").strip()
        if not name:
            continue
        source_key = (row.source_key or "").strip() or f"legacy-name:{name}"
        source_kind = (row.source_kind or "").strip()
        # Carry settings from a superseded legacy (name-based) mapping onto the
        # real source key, so a discovered source key doesn't orphan an existing
        # override and leave a duplicate row behind.
        legacy_key = f"legacy-name:{name}"
        legacy = None
        if source_key != legacy_key:
            legacy = household.moneymoney_account_mappings.filter(source_key=legacy_key).first()
        mapping, created = MoneyMoneyAccountMapping.objects.update_or_create(
            household=household,
            source_key=source_key,
            defaults={
                "source_kind": source_kind,
                "account_name": name,
            },
            create_defaults={
                "source_kind": source_kind,
                "account_name": name,
                "account_type": legacy.account_type if legacy else "",
                "import_enabled": legacy.import_enabled if legacy else True,
                "notes": legacy.notes if legacy else "",
            },
        )
        if created:
            created_count += 1
        elif mapping.source_kind != source_kind or mapping.account_name != name:
            mapping.source_kind = source_kind
            mapping.account_name = name
            mapping.save(update_fields=["source_kind", "account_name", "updated_at"])
        if legacy is not None and legacy.pk != mapping.pk:
            legacy.delete()
        synced_count += 1
    return {"synced_count": synced_count, "created_count": created_count}

def run_moneymoney_diagnostics():
    try:
        connector = MoneyMoneyConnector()
        diagnostics = connector.diagnostics()
    except MoneyMoneyConnectorUnavailable as error:
        diagnostics = {
            "py_money_installed": False,
            "reachable": False,
            "account_count": 0,
            "portfolio_count": 0,
            "position_count": 0,
            "error": str(error),
        }
    except Exception as error:
        diagnostics = {
            "py_money_installed": True,
            "reachable": False,
            "account_count": 0,
            "portfolio_count": 0,
            "position_count": 0,
            "error": str(error),
        }
    else:
        diagnostics = {
            "py_money_installed": diagnostics.py_money_installed,
            "reachable": diagnostics.reachable,
            "account_count": diagnostics.account_count,
            "portfolio_count": diagnostics.portfolio_count,
            "position_count": diagnostics.position_count,
            "error": diagnostics.error,
        }
    return diagnostics

def latest_moneymoney_account_preview(household):
    return household.import_batches.filter(summary__import_kind__in=["moneymoney_accounts"]).first()

def build_moneymoney_mapping_review(household):
    mappings = {mapping.source_key: mapping for mapping in household.moneymoney_account_mappings.all()}
    legacy_by_name = {
        mapping.account_name: mapping
        for mapping in mappings.values()
        if mapping.source_key.startswith("legacy-name:")
    }
    latest_preview = latest_moneymoney_account_preview(household)
    rows_by_key = {}
    if latest_preview:
        for row in (latest_preview.summary or {}).get("rows", []):
            values = row.get("values", {})
            name = (values.get("name") or "").strip()
            source_key = (values.get("source_key") or "").strip() or f"legacy-name:{name}"
            if not name:
                continue
            rows_by_key[source_key] = {
                "source_key": source_key,
                "source_kind": values.get("source_kind", ""),
                "account_name": name,
                "imported_type": values.get("account_type", ""),
                "balance": values.get("balance", ""),
                "currency": values.get("currency", household.currency),
                "as_of_date": values.get("as_of_date", ""),
                "source": "Latest preview",
            }

    # A legacy (name-keyed) mapping for an account now surfaced under a real
    # source key is merged into that preview row rather than shown twice.
    preview_names = {row["account_name"] for row in rows_by_key.values()}
    merged_legacy_keys = {
        mapping.source_key for name, mapping in legacy_by_name.items() if name in preview_names
    }
    account_keys = sorted(
        {*rows_by_key.keys(), *mappings.keys()} - merged_legacy_keys,
        key=lambda key: (rows_by_key.get(key, {}).get("account_name") or mappings[key].account_name).lower(),
    )
    rows = []
    needs_review_count = 0
    import_enabled_count = 0
    disabled_count = 0
    portfolio_count = 0
    for source_key in account_keys:
        preview_row = rows_by_key.get(source_key, {})
        mapping = mappings.get(source_key)
        account_name = preview_row.get("account_name") or (mapping.account_name if mapping else source_key)
        if mapping is None and preview_row:
            mapping = legacy_by_name.get(account_name)
        imported_type = preview_row.get("imported_type", "")
        has_mapping = mapping is not None
        mapped_type = mapping.account_type if mapping else ""
        effective_type = mapped_type or imported_type
        import_enabled = mapping.import_enabled if mapping else True
        if import_enabled:
            import_enabled_count += 1
        else:
            disabled_count += 1
        defaults_to_cash = import_enabled and not mapped_type and imported_type == AssetAccount.AccountType.CASH
        needs_review = defaults_to_cash
        if needs_review:
            needs_review_count += 1
        source_kind = preview_row.get("source_kind", mapping.source_kind if mapping else "")
        if source_kind == "portfolio":
            portfolio_count += 1
        rows.append(
            {
                "source_key": source_key,
                "source_kind": source_kind,
                "account_name": account_name,
                "imported_type": imported_type,
                "mapped_type": mapped_type,
                "effective_type": effective_type,
                "balance": preview_row.get("balance", ""),
                "currency": preview_row.get("currency", household.currency),
                "as_of_date": preview_row.get("as_of_date", ""),
                "source": preview_row.get("source", "Manual override"),
                "has_mapping": has_mapping,
                "import_enabled": import_enabled,
                "needs_review": needs_review,
                "notes": mapping.notes if mapping else "",
            }
        )

    return {
        "rows": rows,
        "latest_preview": latest_preview,
        "mapping_count": len(mappings),
        "import_enabled_count": import_enabled_count,
        "disabled_count": disabled_count,
        "portfolio_count": portfolio_count,
        "needs_review_count": needs_review_count,
        "review_complete": bool(rows) and needs_review_count == 0,
    }
