# MoneyMoney Import

LiF uses `py-money` as the local connector for MoneyMoney data.

Source:

```text
https://github.com/MirkoDziadzka/py-money
```

The connector is intentionally optional. It should only be installed on the Mac that runs MoneyMoney, because `py-money` talks to the MoneyMoney desktop app through AppleScript.

## Current State

The app has a preview-first MoneyMoney path behind the `moneymoney_import` feature flag.
The adapter lives in:

```text
planner/import_adapters/moneymoney.py
```

It maps MoneyMoney objects into the same canonical rows the CSV import already uses:

- `MoneyMoney().accounts()` -> account rows with account type `cash`
- `MoneyMoney().portfolios()` -> account rows with account type `depot`
- `portfolio.positions()` -> depot holding rows

When the feature flag is enabled, the Import Center can create dry-run `ImportBatch` records from MoneyMoney accounts and depot holdings. Nothing is applied automatically.
It can also run connector diagnostics without creating an import batch.
MoneyMoney source accounts and portfolios are tracked by a source key, not just by display name, so duplicate account names can be selected and imported separately.

## Local Install

Install this only in the local checkout that is allowed to access MoneyMoney:

```bash
pipenv install git+https://github.com/MirkoDziadzka/py-money.git#egg=py-money
```

Keep the dependency local until the live connector is mature. The app code imports `money` lazily, so normal development and CI do not require MoneyMoney or AppleScript access.

## Preview Flow

1. Enable `moneymoney_import` in Django admin.
2. Open the Import Center.
3. Run `Run diagnostics`.
4. Review whether `py-money` is installed, MoneyMoney is reachable, and account/portfolio/position counts look plausible.
5. Run `Discover accounts for selection`.
6. Open `MoneyMoney mappings` and choose which discovered accounts or portfolios should be imported.
7. Disable old credit cards, closed accounts, and any account that should stay outside LiF.
8. Run `Preview MoneyMoney accounts`.
9. Apply the clean account batch so depot accounts exist.
10. Run `Preview MoneyMoney depot holdings`.
11. Review account and holding rows.
12. Apply only clean batches.
13. Let the apply step create a backup first.

## Account Selection And Type Overrides

MoneyMoney distinguishes normal accounts from portfolios. LiF maps portfolios to `depot`.
Normal accounts default to `cash`, which is intentionally conservative.

The mapping page stores one row per MoneyMoney source key. Use it to:

- disable accounts or portfolios that should not be imported
- keep duplicate account names separate
- override account types for accounts that should be treated differently

```text
Tagesgeld -> Savings
Verrechnungskonto -> Cash
```

Selections and overrides are stored per household in:

```text
/admin/planner/moneymoneyaccountmapping/
```

After the first MoneyMoney account preview, prefer the mapping page over manual admin entry because it includes the discovered source keys.
The safer workflow is to run discovery before the first account preview. Discovery stores selectable MoneyMoney sources but does not create an import batch and does not change accounts or holdings.

## Mapping Notes

Accounts:

```text
MoneyMoney account.name      -> LiF account name
MoneyMoney account.balance   -> LiF balance
MoneyMoney account.currency  -> LiF currency
MoneyMoney source key        -> LiF moneymoney_account_key
MoneyMoney accounts()        -> LiF account_type cash
MoneyMoney account override  -> LiF account_type cash/savings/loan/other
MoneyMoney portfolios()      -> LiF account_type depot
```

Portfolio positions:

```text
portfolio.name               -> LiF holding account_name
position.name                -> LiF holding name
position.isin                -> LiF holding ISIN
position.type                -> LiF asset class
position.quantity            -> LiF quantity
position.price               -> LiF latest price
position.currencyOfPrice     -> LiF currency
```

MoneyMoney does not provide a payout or maturity date in the documented position surface, so LiF keeps `payout_date` empty for now.
