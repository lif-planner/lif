from dataclasses import dataclass


@dataclass(frozen=True)
class ImportedAccountRow:
    name: str
    account_type: str
    balance: str
    currency: str
    institution: str = ""
    as_of_date: str = ""
    source_key: str = ""
    source_kind: str = ""

    def as_csv_row(self):
        return {
            "name": self.name,
            "account_type": self.account_type,
            "balance": self.balance,
            "currency": self.currency,
            "institution": self.institution,
            "as_of_date": self.as_of_date,
            "source_key": self.source_key,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True)
class ImportedDepotHoldingRow:
    account_name: str
    name: str
    isin: str
    ticker: str
    asset_class: str
    quantity: str
    latest_price: str
    currency: str
    as_of_date: str = ""
    payout_date: str = ""
    payout_amount: str = ""
    account_source_key: str = ""

    def as_csv_row(self):
        return {
            "account_name": self.account_name,
            "name": self.name,
            "isin": self.isin,
            "ticker": self.ticker,
            "asset_class": self.asset_class,
            "quantity": self.quantity,
            "latest_price": self.latest_price,
            "currency": self.currency,
            "as_of_date": self.as_of_date,
            "payout_date": self.payout_date,
            "payout_amount": self.payout_amount,
            "account_source_key": self.account_source_key,
        }
