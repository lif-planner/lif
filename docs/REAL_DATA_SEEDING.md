# Real Data Seeding

Do not put real family, salary, account, or depot data in Git. Keep it in a private local folder and run it against the real checkout when needed.

Recommended folder:

```text
~/Private/LiF-data/
  seed_real.py
  imports/
    accounts.csv
    holdings.csv
  backups/
  notes.md
```

`local_private/` is also ignored by this repository for clone-local scratch files, but a folder outside the repo is safer and easier to back up separately.

## What Belongs Where

Use a private seed script for personal structure and assumptions:

- household settings
- people and family structure
- recurring salary rules
- Kindergeld or other family income
- recurring expenses
- cash goals
- retirement plans
- salary changes
- private pension assumptions

Use imports for data that comes from other systems:

- account balances
- depot holdings
- future MoneyMoney exports
- future YNAB foundation data

## First-Time Setup

Create the private folder:

```bash
mkdir -p ~/Private/LiF-data/imports
cp examples/seed_real.example.py ~/Private/LiF-data/seed_real.py
cp examples/accounts.example.csv ~/Private/LiF-data/imports/accounts.csv
cp examples/accounts_update.example.csv ~/Private/LiF-data/imports/accounts_update.csv
cp examples/depot_holdings.example.csv ~/Private/LiF-data/imports/depot_holdings.csv
cp examples/depot_holdings_update.example.csv ~/Private/LiF-data/imports/depot_holdings_update.csv
```

Edit the private copies:

```bash
nano ~/Private/LiF-data/seed_real.py
nano ~/Private/LiF-data/imports/accounts.csv
nano ~/Private/LiF-data/imports/depot_holdings.csv
```

Run the seed from the LiF checkout:

```bash
cd ~/Services/LiF
pipenv run python manage.py backup_data --label before-real-seed
pipenv run python manage.py shell < ~/Private/LiF-data/seed_real.py
pipenv run python manage.py smoke_test
```

Then use the Import Center for CSV dry-runs:

```text
/imports/
```

The accounts and depot holdings imports are intentionally preview-first. Upload the CSV, review the dry-run table, then apply only a clean batch. Applying a batch creates a database backup first.

Account rows are matched by account name:

- a new name creates an account
- an existing name updates account type, balance, currency, institution, and as-of date
- fields outside the CSV, such as notes and future YNAB IDs, are preserved

Depot holding rows require an existing account with account type `depot`. They are matched by depot account plus ISIN when present, then ticker, then holding name. Fields outside the CSV, such as notes, are preserved.

## Repeatable Seeds

Private seed scripts should use `update_or_create`, not plain `create`, so rerunning the seed updates existing records instead of duplicating them.

Good pattern:

```python
MoneyRule.objects.update_or_create(
    household=household,
    name="Salary Parent 1",
    defaults={
        "kind": MoneyRule.Kind.INCOME,
        "amount": Decimal("4500.00"),
    },
)
```

Avoid:

```python
MoneyRule.objects.create(...)
```

## Safety Checklist

Before running a private seed:

```bash
pipenv run python manage.py backup_data --label before-real-seed
python3 scripts/scan_secrets.py
git status --short
```

After running it:

```bash
pipenv run python manage.py smoke_test
```

Open:

```text
/quality/
```

Review any missing assumptions or stale values before relying on projections.

## Backup Advice

Back up `~/Private/LiF-data/` with encrypted local backup tooling:

- encrypted Time Machine disk
- encrypted external drive
- encrypted archive
- private device-to-device sync

Avoid pushing this folder to GitHub, even as a private repository.
