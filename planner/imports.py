import csv
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from .feature_flags import feature_enabled
from .models import AssetAccount, DepotHolding, ImportBatch


ACCOUNT_COLUMNS = ["name", "account_type", "balance", "currency", "institution", "as_of_date"]
ACCOUNT_SOURCE_FIELDS = ["source_key", "source_kind"]
DEPOT_HOLDING_COLUMNS = [
    "account_name",
    "name",
    "isin",
    "ticker",
    "asset_class",
    "quantity",
    "latest_price",
    "currency",
    "as_of_date",
    "payout_date",
]
DEPOT_HOLDING_OPTIONAL_COLUMNS = ["payout_amount"]


@dataclass(frozen=True)
class AccountImportRow:
    row_number: int
    action: str
    values: dict
    errors: list
    warnings: list
    existing_values: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)

    @property
    def is_valid(self):
        return not self.errors

    @property
    def has_warnings(self):
        return bool(self.warnings)

    @property
    def status(self):
        if self.errors:
            return "error"
        if self.warnings:
            return "warning"
        return self.action


@dataclass(frozen=True)
class DepotHoldingImportRow:
    row_number: int
    action: str
    match_key: dict
    values: dict
    errors: list
    warnings: list
    existing_values: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)

    @property
    def is_valid(self):
        return not self.errors

    @property
    def has_warnings(self):
        return bool(self.warnings)

    @property
    def status(self):
        if self.errors:
            return "error"
        if self.warnings:
            return "warning"
        return self.action


def decode_csv(uploaded_file):
    raw = uploaded_file.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8-sig")


def parse_decimal(value):
    try:
        return Decimal((value or "0").strip())
    except InvalidOperation:
        return None


def parse_optional_decimal(value):
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    return parse_decimal(cleaned)


def parse_date(value):
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date()
    except ValueError:
        return "invalid"


def normalize_account_type(value):
    cleaned = (value or "").strip().lower()
    valid_types = {choice for choice, label in AssetAccount.AccountType.choices}
    return cleaned if cleaned in valid_types else None


def account_csv_dry_run(household, uploaded_file):
    text = decode_csv(uploaded_file)
    reader = csv.DictReader(StringIO(text))
    missing_columns = [column for column in ACCOUNT_COLUMNS if column not in (reader.fieldnames or [])]

    if missing_columns:
        return {
            "rows": [],
            "missing_columns": missing_columns,
            "row_count": 0,
            "valid_count": 0,
            "warning_count": 0,
            "error_count": 1,
            "action_counts": {"create": 0, "update": 0, "unchanged": 0, "warning": 0, "error": 1},
        }

    return account_rows_dry_run(household, reader)


def account_values_unchanged(account, values):
    return (
        account.account_type == values["account_type"]
        and account.balance == Decimal(values["balance"])
        and account.currency == values["currency"]
        and account.institution == values["institution"]
        and (account.as_of_date.isoformat() if account.as_of_date else "") == values["as_of_date"]
        # Only compare the source key when the incoming row actually carries one
        # (MoneyMoney). A CSV row has no source key and must not flag a change.
        and (not values.get("source_key") or account.moneymoney_account_key == values["source_key"])
    )


def account_existing_values(account):
    if account is None:
        return {}
    return {
        "name": account.name,
        "account_type": account.account_type,
        "balance": str(account.balance),
        "currency": account.currency,
        "institution": account.institution,
        "as_of_date": account.as_of_date.isoformat() if account.as_of_date else "",
        "source_key": account.moneymoney_account_key,
    }


def changed_values(existing_values, new_values, fields):
    return {
        field: {"old": existing_values.get(field, ""), "new": new_values.get(field, "")}
        for field in fields
        if existing_values.get(field, "") != new_values.get(field, "")
    }


def import_result_counts(rows):
    action_counts = {"create": 0, "update": 0, "unchanged": 0, "warning": 0, "error": 0}
    for row in rows:
        if row.errors:
            action_counts["error"] += 1
        elif row.warnings:
            action_counts["warning"] += 1
        else:
            action_counts[row.action] = action_counts.get(row.action, 0) + 1
    valid_count = sum(1 for row in rows if row.is_valid)
    warning_count = sum(1 for row in rows if row.has_warnings)
    error_count = len(rows) - valid_count
    return {
        "valid_count": valid_count,
        "warning_count": warning_count,
        "error_count": error_count,
        "action_counts": action_counts,
    }


def account_rows_dry_run(household, raw_rows, start_row_number=2):
    rows = []
    existing_accounts = {account.name.strip().lower(): account for account in household.accounts.all()}
    existing_moneymoney_accounts = {
        account.moneymoney_account_key: account
        for account in household.accounts.exclude(moneymoney_account_key="")
    }
    existing_by_institution = {}
    for account in household.accounts.all():
        if account.institution:
            existing_by_institution.setdefault((account.institution.strip().lower(), account.account_type), []).append(account)
    seen_names = {}
    for index, raw_row in enumerate(raw_rows, start=start_row_number):
        errors = []
        warnings = []
        name = (raw_row.get("name") or "").strip()
        account_type = normalize_account_type(raw_row.get("account_type"))
        balance = parse_decimal(raw_row.get("balance"))
        currency = (raw_row.get("currency") or household.currency).strip().upper() or household.currency
        institution = (raw_row.get("institution") or "").strip()
        as_of_date = parse_date(raw_row.get("as_of_date"))
        source_key = (raw_row.get("source_key") or "").strip()
        source_kind = (raw_row.get("source_kind") or "").strip()

        if not name:
            errors.append("Name is required.")
        if account_type is None:
            errors.append("Account type must be one of cash, savings, depot, loan, other.")
        if balance is None:
            errors.append("Balance must be a decimal number.")
        if len(currency) != 3:
            errors.append("Currency must be a three-letter code.")
        if as_of_date == "invalid":
            errors.append("As-of date must use YYYY-MM-DD.")

        account_key = name.lower()
        # MoneyMoney accounts are identified by source key, so distinct accounts
        # that legitimately share a name are not duplicates. Only warn when the
        # identifying key (source key, else name) actually repeats.
        dedupe_key = source_key or account_key
        if dedupe_key:
            if dedupe_key in seen_names:
                warnings.append(f"Duplicate account name in this file; first seen on row {seen_names[dedupe_key]}.")
            else:
                seen_names[dedupe_key] = index

        values = {
            "name": name,
            "account_type": account_type or "",
            "balance": str(balance) if balance is not None else raw_row.get("balance", ""),
            "currency": currency,
            "institution": institution,
            "as_of_date": as_of_date.isoformat() if as_of_date and as_of_date != "invalid" else raw_row.get("as_of_date", ""),
            "source_key": source_key,
            "source_kind": source_kind,
        }
        existing = existing_moneymoney_accounts.get(source_key) if source_key else existing_accounts.get(account_key)
        existing_values = account_existing_values(existing)
        if existing:
            action = "unchanged" if not errors and account_values_unchanged(existing, values) else "update"
        else:
            action = "create"
            if institution and account_type:
                similar_accounts = existing_by_institution.get((institution.lower(), account_type), [])
                if similar_accounts:
                    names = ", ".join(account.name for account in similar_accounts[:3])
                    warnings.append(f"Possible duplicate: {institution} already has {account_type} account(s): {names}.")
        rows.append(
            AccountImportRow(
                row_number=index,
                action=action,
                values=values,
                errors=errors,
                warnings=warnings,
                existing_values=existing_values,
                changes=changed_values(
                    existing_values,
                    values,
                    ["account_type", "balance", "currency", "institution", "as_of_date", "source_key"],
                ),
            )
        )

    counts = import_result_counts(rows)
    return {
        "rows": rows,
        "missing_columns": [],
        "row_count": len(rows),
        **counts,
    }


def holding_account_key(account=None, account_name="", account_source_key=""):
    if account_source_key:
        return f"moneymoney:{account_source_key}"
    if account and account.moneymoney_account_key:
        return f"moneymoney:{account.moneymoney_account_key}"
    return f"name:{(account_name or '').strip().lower()}"


def holding_match_key(account_name, isin, ticker, name, account=None, account_source_key=""):
    account_key = holding_account_key(account=account, account_name=account_name, account_source_key=account_source_key)
    if isin:
        return {"account_name": account_key, "field": "isin", "value": isin.strip().upper()}
    if ticker:
        return {"account_name": account_key, "field": "ticker", "value": ticker.strip().upper()}
    return {"account_name": account_key, "field": "name", "value": (name or "").strip().lower()}


def depot_holding_lookup(existing_holdings, match_key):
    return existing_holdings.get((match_key["account_name"], match_key["field"], match_key["value"]))


def depot_holding_index(household):
    existing_holdings = {}
    holdings = DepotHolding.objects.filter(asset_account__household=household).select_related("asset_account")
    for holding in holdings:
        account_key = holding_account_key(account=holding.asset_account, account_name=holding.asset_account.name)
        for field, value in [
            ("isin", holding.isin.strip().upper()),
            ("ticker", holding.ticker.strip().upper()),
            ("name", holding.name.strip().lower()),
        ]:
            if value:
                existing_holdings[(account_key, field, value)] = holding
    return existing_holdings


def depot_holding_csv_dry_run(household, uploaded_file):
    text = decode_csv(uploaded_file)
    reader = csv.DictReader(StringIO(text))
    missing_columns = [column for column in DEPOT_HOLDING_COLUMNS if column not in (reader.fieldnames or [])]

    if missing_columns:
        return {
            "rows": [],
            "missing_columns": missing_columns,
            "row_count": 0,
            "valid_count": 0,
            "warning_count": 0,
            "error_count": 1,
            "action_counts": {"create": 0, "update": 0, "unchanged": 0, "warning": 0, "error": 1},
        }

    return depot_holding_rows_dry_run(household, reader)


def depot_holding_values_unchanged(holding, values):
    return (
        holding.name == values["name"]
        and holding.isin == values["isin"]
        and holding.ticker == values["ticker"]
        and holding.asset_class == values["asset_class"]
        and holding.quantity == Decimal(values["quantity"])
        and holding.latest_price == Decimal(values["latest_price"])
        and holding.currency == values["currency"]
        and (holding.as_of_date.isoformat() if holding.as_of_date else "") == values["as_of_date"]
        and (holding.payout_date.isoformat() if holding.payout_date else "") == values["payout_date"]
        and (str(holding.payout_amount) if holding.payout_amount is not None else "") == values.get("payout_amount", "")
    )


def depot_holding_existing_values(holding):
    if holding is None:
        return {}
    return {
        "account_name": holding.asset_account.name,
        "name": holding.name,
        "isin": holding.isin,
        "ticker": holding.ticker,
        "asset_class": holding.asset_class,
        "quantity": str(holding.quantity),
        "latest_price": str(holding.latest_price),
        "current_value": str(holding.current_value),
        "currency": holding.currency,
        "as_of_date": holding.as_of_date.isoformat() if holding.as_of_date else "",
        "payout_date": holding.payout_date.isoformat() if holding.payout_date else "",
        "payout_amount": str(holding.payout_amount) if holding.payout_amount is not None else "",
    }


def depot_holding_rows_dry_run(household, raw_rows, start_row_number=2):
    rows = []
    depot_accounts = {
        account.name.strip().lower(): account
        for account in household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT)
    }
    depot_accounts_by_source_key = {
        account.moneymoney_account_key: account
        for account in household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT).exclude(moneymoney_account_key="")
    }
    existing_holdings = depot_holding_index(household)
    seen_match_keys = {}

    for index, raw_row in enumerate(raw_rows, start=start_row_number):
        errors = []
        warnings = []
        account_name = (raw_row.get("account_name") or "").strip()
        name = (raw_row.get("name") or "").strip()
        isin = (raw_row.get("isin") or "").strip().upper()
        ticker = (raw_row.get("ticker") or "").strip().upper()
        asset_class = (raw_row.get("asset_class") or "ETF").strip() or "ETF"
        quantity = parse_decimal(raw_row.get("quantity"))
        latest_price = parse_decimal(raw_row.get("latest_price"))
        currency = (raw_row.get("currency") or household.currency).strip().upper() or household.currency
        as_of_date = parse_date(raw_row.get("as_of_date"))
        payout_date = parse_date(raw_row.get("payout_date"))
        payout_amount = parse_optional_decimal(raw_row.get("payout_amount"))
        account_source_key = (raw_row.get("account_source_key") or "").strip()
        account = depot_accounts_by_source_key.get(account_source_key) if account_source_key else depot_accounts.get(account_name.lower())

        if not account_name:
            errors.append("Depot account name is required.")
        elif account is None:
            errors.append("Depot account must already exist and have account type depot.")
        if not name:
            errors.append("Holding name is required.")
        if quantity is None:
            errors.append("Quantity must be a decimal number.")
        elif quantity < Decimal("0"):
            errors.append("Quantity cannot be negative.")
        if latest_price is None:
            errors.append("Latest price must be a decimal number.")
        elif latest_price < Decimal("0"):
            errors.append("Latest price cannot be negative.")
        if len(currency) != 3:
            errors.append("Currency must be a three-letter code.")
        if as_of_date == "invalid":
            errors.append("As-of date must use YYYY-MM-DD.")
        if payout_date == "invalid":
            errors.append("Payout date must use YYYY-MM-DD.")
        if raw_row.get("payout_amount") not in {None, ""} and payout_amount is None:
            errors.append("Payout amount must be a decimal number.")
        elif payout_amount is not None and payout_amount < Decimal("0"):
            errors.append("Payout amount cannot be negative.")

        match_key = holding_match_key(account_name, isin, ticker, name, account=account, account_source_key=account_source_key)
        match_tuple = (match_key["account_name"], match_key["field"], match_key["value"])
        if match_tuple in seen_match_keys:
            warnings.append(f"Duplicate holding match key in this file; first seen on row {seen_match_keys[match_tuple]}.")
        else:
            seen_match_keys[match_tuple] = index

        values = {
            "account_name": account_name,
            "name": name,
            "isin": isin,
            "ticker": ticker,
            "asset_class": asset_class,
            "quantity": str(quantity) if quantity is not None else raw_row.get("quantity", ""),
            "latest_price": str(latest_price) if latest_price is not None else raw_row.get("latest_price", ""),
            "currency": currency,
            "as_of_date": as_of_date.isoformat() if as_of_date and as_of_date != "invalid" else raw_row.get("as_of_date", ""),
            "payout_date": payout_date.isoformat() if payout_date and payout_date != "invalid" else raw_row.get("payout_date", ""),
            "payout_amount": str(payout_amount) if payout_amount is not None else (raw_row.get("payout_amount") or ""),
            "account_source_key": account_source_key,
        }
        existing_holding = depot_holding_lookup(existing_holdings, match_key)
        existing_values = depot_holding_existing_values(existing_holding)
        new_values_with_market_value = {
            **values,
            "current_value": str(quantity * latest_price) if quantity is not None and latest_price is not None else "",
        }
        if existing_holding:
            action = "unchanged" if not errors and depot_holding_values_unchanged(existing_holding, values) else "update"
        else:
            action = "create"
            if isin:
                same_isin = [
                    holding
                    for (account_key, field, value), holding in existing_holdings.items()
                    if field == "isin" and value == isin and account_key != match_key["account_name"]
                ]
                if same_isin:
                    accounts = ", ".join(holding.asset_account.name for holding in same_isin[:3])
                    warnings.append(f"Same ISIN already exists in another depot: {accounts}.")
        rows.append(
            DepotHoldingImportRow(
                row_number=index,
                action=action,
                match_key=match_key,
                values=values,
                errors=errors,
                warnings=warnings,
                existing_values=existing_values,
                changes=changed_values(
                    existing_values,
                    new_values_with_market_value,
                    [
                        "name",
                        "isin",
                        "ticker",
                        "asset_class",
                        "quantity",
                        "latest_price",
                        "current_value",
                        "currency",
                        "as_of_date",
                        "payout_date",
                        "payout_amount",
                    ],
                ),
            )
        )

    counts = import_result_counts(rows)
    return {
        "rows": rows,
        "missing_columns": [],
        "row_count": len(rows),
        **counts,
    }


def dry_run_summary(result):
    return {
        "missing_columns": result["missing_columns"],
        "rows": [
            {
                "row_number": row.row_number,
                "action": row.action,
                "match_key": getattr(row, "match_key", {}),
                "values": row.values,
                "errors": row.errors,
                "warnings": row.warnings,
                "existing_values": row.existing_values,
                "changes": row.changes,
            }
            for row in result["rows"]
        ],
        "action_counts": result.get("action_counts", {}),
        "warning_count": result.get("warning_count", 0),
    }


def apply_account_import_batch(batch, create_backup=True, pre_apply_snapshot=None):
    if batch.source == ImportBatch.Source.MONEYMONEY:
        if batch.summary.get("import_kind") != "moneymoney_accounts":
            raise ValueError("Only MoneyMoney account batches can be applied with the account importer.")
    elif batch.source != ImportBatch.Source.CSV_ACCOUNTS:
        raise ValueError("Only account import batches can be applied.")
    if batch.status != ImportBatch.Status.DRY_RUN:
        raise ValueError("Only unapplied dry-run batches can be applied.")
    if batch.error_count:
        raise ValueError("Import batches with validation errors cannot be applied.")
    if batch.summary.get("missing_columns"):
        raise ValueError("Import batches with missing columns cannot be applied.")
    if feature_enabled("read_only_mode"):
        raise ValueError("Read-only mode is active.")

    backup_label = f"before-import-{batch.pk}" if create_backup else ""
    if create_backup:
        call_command("backup_data", label=backup_label)

    created_count = 0
    updated_count = 0
    rows = batch.summary.get("rows", [])
    with transaction.atomic():
        for row in rows:
            if row.get("errors"):
                raise ValueError("Import batches with validation errors cannot be applied.")
            if row.get("action") == "unchanged":
                continue
            values = row.get("values", {})
            name = (values.get("name") or "").strip()
            if not name:
                raise ValueError("Import row is missing an account name.")

            defaults = {
                "account_type": values["account_type"],
                "balance": Decimal(values["balance"]),
                "currency": values["currency"],
                "source": AssetAccount.Source.MONEYMONEY if batch.source == ImportBatch.Source.MONEYMONEY else AssetAccount.Source.MANUAL,
                "institution": values.get("institution", ""),
                "as_of_date": parse_date(values.get("as_of_date")),
            }
            # Only MoneyMoney batches own the source key. Non-MoneyMoney imports
            # leave it untouched so a CSV re-import matched by name does not wipe
            # an existing MoneyMoney link (and create a duplicate next sync).
            if batch.source == ImportBatch.Source.MONEYMONEY:
                defaults["moneymoney_account_key"] = values.get("source_key", "")
            lookup = {"household": batch.household}
            if batch.source == ImportBatch.Source.MONEYMONEY and values.get("source_key"):
                lookup["moneymoney_account_key"] = values["source_key"]
                defaults["name"] = name
            else:
                lookup["name"] = name
            account, created = AssetAccount.objects.update_or_create(**lookup, defaults=defaults)
            if created:
                created_count += 1
            else:
                updated_count += 1

        summary = dict(batch.summary)
        summary["apply_result"] = {
            "created_count": created_count,
            "updated_count": updated_count,
            "skipped_count": sum(1 for row in rows if row.get("action") == "unchanged"),
            "backup_label": backup_label,
            "pre_apply_snapshot_id": pre_apply_snapshot.pk if pre_apply_snapshot else None,
            "pre_apply_snapshot_name": pre_apply_snapshot.name if pre_apply_snapshot else "",
            "applied_at": timezone.now().isoformat(),
        }
        batch.summary = summary
        batch.status = ImportBatch.Status.APPLIED
        batch.save(update_fields=["summary", "status"])

    return {
        "created_count": created_count,
        "updated_count": updated_count,
    }


def apply_depot_holding_import_batch(batch, create_backup=True, pre_apply_snapshot=None):
    if batch.source == ImportBatch.Source.MONEYMONEY:
        if batch.summary.get("import_kind") != "moneymoney_depot_holdings":
            raise ValueError("Only MoneyMoney depot holding batches can be applied with the holding importer.")
    elif batch.source != ImportBatch.Source.CSV_DEPOT_HOLDINGS:
        raise ValueError("Only depot holding import batches can be applied.")
    if batch.status != ImportBatch.Status.DRY_RUN:
        raise ValueError("Only unapplied dry-run batches can be applied.")
    if batch.error_count:
        raise ValueError("Import batches with validation errors cannot be applied.")
    if batch.summary.get("missing_columns"):
        raise ValueError("Import batches with missing columns cannot be applied.")
    if feature_enabled("read_only_mode"):
        raise ValueError("Read-only mode is active.")

    backup_label = f"before-import-{batch.pk}" if create_backup else ""
    if create_backup:
        call_command("backup_data", label=backup_label)

    created_count = 0
    updated_count = 0
    rows = batch.summary.get("rows", [])
    with transaction.atomic():
        depot_accounts = {
            account.name.strip().lower(): account
            for account in batch.household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT)
        }
        depot_accounts_by_source_key = {
            account.moneymoney_account_key: account
            for account in batch.household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT).exclude(moneymoney_account_key="")
        }
        existing_holdings = depot_holding_index(batch.household)

        for row in rows:
            if row.get("errors"):
                raise ValueError("Import batches with validation errors cannot be applied.")
            if row.get("action") == "unchanged":
                continue
            values = row.get("values", {})
            account_name = (values.get("account_name") or "").strip()
            account_source_key = (values.get("account_source_key") or "").strip()
            account = (
                depot_accounts_by_source_key.get(account_source_key)
                if account_source_key
                else depot_accounts.get(account_name.lower())
            )
            if account is None:
                raise ValueError(f"Depot account does not exist: {account_name}")

            match_key = row.get("match_key") or holding_match_key(
                account_name,
                values.get("isin", ""),
                values.get("ticker", ""),
                values.get("name", ""),
                account=account,
                account_source_key=account_source_key,
            )
            holding = depot_holding_lookup(existing_holdings, match_key)
            defaults = {
                "asset_account": account,
                "name": (values.get("name") or "").strip(),
                "isin": (values.get("isin") or "").strip().upper(),
                "ticker": (values.get("ticker") or "").strip().upper(),
                "asset_class": (values.get("asset_class") or "ETF").strip() or "ETF",
                "quantity": Decimal(values["quantity"]),
                "latest_price": Decimal(values["latest_price"]),
                "currency": values["currency"],
                "as_of_date": parse_date(values.get("as_of_date")),
                "payout_date": parse_date(values.get("payout_date")),
                "payout_amount": Decimal(values["payout_amount"]) if values.get("payout_amount") else None,
            }
            if holding:
                for field, value in defaults.items():
                    setattr(holding, field, value)
                holding.save(update_fields=[*defaults.keys(), "updated_at"])
                updated_count += 1
            else:
                holding = DepotHolding.objects.create(**defaults)
                existing_holdings[
                    (
                        holding_account_key(account=account, account_name=account.name),
                        match_key["field"],
                        match_key["value"],
                    )
                ] = holding
                created_count += 1

        summary = dict(batch.summary)
        summary["apply_result"] = {
            "created_count": created_count,
            "updated_count": updated_count,
            "skipped_count": sum(1 for row in rows if row.get("action") == "unchanged"),
            "backup_label": backup_label,
            "pre_apply_snapshot_id": pre_apply_snapshot.pk if pre_apply_snapshot else None,
            "pre_apply_snapshot_name": pre_apply_snapshot.name if pre_apply_snapshot else "",
            "applied_at": timezone.now().isoformat(),
        }
        batch.summary = summary
        batch.status = ImportBatch.Status.APPLIED
        batch.save(update_fields=["summary", "status"])

    return {
        "created_count": created_count,
        "updated_count": updated_count,
    }
