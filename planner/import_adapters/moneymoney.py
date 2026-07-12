from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from planner.models import AssetAccount

from .base import ImportedAccountRow, ImportedDepotHoldingRow


class MoneyMoneyConnectorUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MoneyMoneyDiagnostics:
    py_money_installed: bool
    reachable: bool
    account_count: int = 0
    portfolio_count: int = 0
    position_count: int = 0
    error: str = ""


def decimal_string(value, places="0.01"):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        number = Decimal("0")
    return str(number.quantize(Decimal(places)))


def optional_text(value):
    return "" if value is None else str(value).strip()


def source_identity(prefix, item, index):
    for attribute in ["id", "uuid", "accountId", "account_id", "portfolioId", "portfolio_id", "number"]:
        value = getattr(item, attribute, "")
        if value:
            return f"{prefix}:{value}"
    return f"{prefix}:index:{index}:{optional_text(getattr(item, 'name', ''))}"


def money_module():
    try:
        import money
    except ImportError as error:
        raise MoneyMoneyConnectorUnavailable(
            "py-money is not installed. Install it only on the local Mac that can access MoneyMoney."
        ) from error
    return money


def py_money_installed():
    try:
        money_module()
    except MoneyMoneyConnectorUnavailable:
        return False
    return True


class MoneyMoneyConnector:
    def __init__(self, client=None):
        if client is None:
            client = money_module().MoneyMoney()
        self.client = client

    def diagnostics(self):
        try:
            accounts = list(self.client.accounts())
            portfolios = list(self.client.portfolios())
            position_count = sum(len(list(portfolio.positions())) for portfolio in portfolios)
        except Exception as error:
            return MoneyMoneyDiagnostics(
                py_money_installed=py_money_installed(),
                reachable=False,
                error=str(error),
            )
        return MoneyMoneyDiagnostics(
            py_money_installed=True,
            reachable=True,
            account_count=len(accounts),
            portfolio_count=len(portfolios),
            position_count=position_count,
        )

    def account_rows(self, as_of_date=None, account_type_overrides=None):
        account_type_overrides = account_type_overrides or {}
        date_text = (as_of_date or timezone.localdate()).isoformat()
        rows = []
        for index, account in enumerate(self.client.accounts()):
            source_key = source_identity("account", account, index)
            # Fall back to a name lookup so overrides saved before a source key
            # was known (migrated/legacy or pre-preview manual mappings) still
            # apply instead of silently reverting to the default.
            account_type = (
                account_type_overrides.get(source_key)
                or account_type_overrides.get(account.name)
                or AssetAccount.AccountType.CASH
            )
            rows.append(
                ImportedAccountRow(
                    name=account.name,
                    account_type=account_type,
                    balance=decimal_string(account.balance),
                    currency=account.currency,
                    as_of_date=date_text,
                    source_key=source_key,
                    source_kind="account",
                )
            )
        for index, portfolio in enumerate(self.client.portfolios()):
            source_key = source_identity("portfolio", portfolio, index)
            account_type = (
                account_type_overrides.get(source_key)
                or account_type_overrides.get(portfolio.name)
                or AssetAccount.AccountType.DEPOT
            )
            rows.append(
                ImportedAccountRow(
                    name=portfolio.name,
                    account_type=account_type,
                    balance=decimal_string(portfolio.balance),
                    currency=portfolio.currency,
                    as_of_date=date_text,
                    source_key=source_key,
                    source_kind="portfolio",
                )
            )
        return rows

    def depot_holding_rows(self, as_of_date=None, selected_source_keys=None):
        selected_source_keys = set(selected_source_keys or [])
        date_text = (as_of_date or timezone.localdate()).isoformat()
        rows = []
        for index, portfolio in enumerate(self.client.portfolios()):
            source_key = source_identity("portfolio", portfolio, index)
            if selected_source_keys and source_key not in selected_source_keys:
                continue
            for position in portfolio.positions():
                rows.append(
                    ImportedDepotHoldingRow(
                        account_name=portfolio.name,
                        name=optional_text(position.name),
                        isin=optional_text(position.isin).upper(),
                        ticker="",
                        asset_class=optional_text(position.type) or "Security",
                        quantity=decimal_string(position.quantity, places="0.000001"),
                        latest_price=decimal_string(position.price),
                        currency=optional_text(position.currencyOfPrice or position.currencyOfAmount or portfolio.currency).upper(),
                        as_of_date=date_text,
                        account_source_key=source_key,
                    )
                )
        return rows
