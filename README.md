# LiF Planner

LiF is a local-first household and retirement planning tool for people who want
long-range financial planning without sending private family data to a hosted
service.

It is built with Python and Django and focuses on German-style household
planning: family structure, children, Kindergeld, mortgages, depot growth,
pensions, savings, taxes on capital income, and long-term cash-flow decisions.
The app is meant to run on your own computer, NAS, Proxmox LXC, or private
server behind VPN/LAN access.

## Why LiF?

- **Local-first by design:** no telemetry, no hosted sync, and no third-party
  finance API by default.
- **Future planning, not budgeting clone:** current account data can be the
  foundation, but the main question is what happens over years and decades.
- **German household assumptions:** Kindergeld, Rentenpunkte, Direktversicherung
  style pensions, depot taxation assumptions, mortgages, and property transfers
  are first-class planning concepts.
- **Auditable projections:** monthly and yearly forecast pages explain how cash,
  depot values, debt, net worth, and retirement outcomes move.
- **Safe demo path:** synthetic seed data lets you explore the full app before
  entering private numbers.

## What It Models

- Adults, children, child income, child milestones, salary changes, and planned
  life events.
- Cash, savings, depot, loan, real-estate, child-owned, and excluded accounts.
- Depot holdings, planned investments, bond maturity payouts, distributions,
  average return assumptions, and transfers from savings into investments.
- Mortgages, fixed-interest periods, refinance assumptions, extra payments, and
  private loans.
- Solar or other dated income investments.
- German statutory pension-style retirement assumptions plus private pensions.
- RSU/equity grants, yearly bonuses, recurring income, true expenses, and cash
  goals for FIRE-style planning.
- Scenarios, data confidence, reconciliation, snapshots, annual reviews, and
  projection integrity checks.

## Documentation

- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) -- using LiF: core concepts,
  getting started, and a feature-by-feature walkthrough.
- [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md) -- self-hosting LiF: choosing a
  deployment method, configuration, backups, and updates.
- [CONTRIBUTING.md](CONTRIBUTING.md) -- local setup, test expectations, and
  contribution guardrails.
- [SECURITY.md](SECURITY.md) -- local-first threat model and vulnerability
  reporting guidance.
- [CHANGELOG.md](CHANGELOG.md) -- public release notes and version history.
- [NOTICE.md](NOTICE.md) -- notable bundled third-party notices.
- [docs/RELEASE.md](docs/RELEASE.md) -- maintainer checklist for creating the
  clean public `v1.0.0` release repository.

The rest of this README covers local development setup and the technical
configuration reference. For day-to-day usage, start with the user guide.

## License

LiF is released under the MIT license. The goal is to make local self-hosting,
forks, contributions, and private adaptations simple.

## Prerequisites

For the local demo and development setup, install:

- Python 3.12 or newer.
- Pipenv for managing the Python virtual environment.
- Git for cloning and updating the repository.

On macOS with Homebrew:

```bash
brew install python pipenv git
```

On Debian/Ubuntu:

```bash
sudo apt update
sudo apt install python3 python3-pip git
python3 -m pip install --user pipenv
```

If `pipenv` is installed with `pip --user`, make sure your user-level Python
bin directory is on `PATH` before running the commands below.

## Quick Demo

Install dependencies, migrate, load synthetic demo data, and run the app:

```bash
pipenv install
pipenv run python manage.py migrate
pipenv run python manage.py seed_demo
pipenv run python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/`.

The demo data is synthetic. Do not enter real household data into a checkout
unless you have decided how it is deployed, backed up, and protected.

## Local Setup

Install dependencies:

```bash
pipenv install
```

Apply migrations:

```bash
pipenv run python manage.py migrate
```

Create demo data:

```bash
pipenv run python manage.py seed_demo
```

Run the app:

```bash
pipenv run python manage.py runserver 127.0.0.1:8000
```

On a fresh checkout without demo data, the dashboard redirects to `/setup/` so
you can create the local household foundation before importing or entering
sensitive account data.

For manual account entry, use the guided account setup:

```text
/setup/accounts/
```

It can create cash, savings, depot, and loan accounts, including optional first depot holding data and mortgage repayment details.

## Tests

```bash
pipenv run python manage.py test
pipenv run python manage.py check
```

## Container Deployment

For Proxmox LXC or another Linux container host, use the Docker Compose setup in
[docs/CONTAINER_DEPLOYMENT.md](docs/CONTAINER_DEPLOYMENT.md). It stores SQLite,
backups, and collected static files under a persistent `data/` directory.
To automate setup and updates from this machine, see
[docs/ANSIBLE_DEPLOYMENT.md](docs/ANSIBLE_DEPLOYMENT.md).
The Ansible path publishes LiF on port `80` by default.

## Secret Checks

Enable the local pre-commit hook once per clone:

```bash
git config core.hooksPath .githooks
```

The hook runs a dependency-free staged-file scan before every commit:

```bash
python3 scripts/scan_secrets.py --staged
```

You can also scan all tracked files manually:

```bash
python3 scripts/scan_secrets.py
```

Before a public export, also run:

```bash
python3 scripts/scan_public_readiness.py
./scripts/simulate_public_checkout.sh
```

## Configuration

The app is intended to run locally. Settings can be overridden with:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS` (full origins, e.g. `https://lif.example.com`; only needed behind a reverse proxy/tunnel on its own hostname)
- `DJANGO_TRUST_PROXY_SSL_HEADER` (set `1` only behind a trusted TLS-terminating proxy/tunnel)
- `DJANGO_DB_PATH`
- `DJANGO_STATIC_ROOT`
- `LIF_REQUIRE_LOGIN`
- `LIF_BACKUP_DIR`
- `LIF_VERSION`
- `LIF_GIT_COMMIT`

`LIF_REQUIRE_LOGIN` defaults to off so demo and development checkouts stay frictionless. For any checkout that contains real household data, enable it and create a local Django user:

```bash
export LIF_REQUIRE_LOGIN=1
pipenv run python manage.py createsuperuser
```

When enabled, all planner pages require login before showing data. `/health/`, `/login/`, `/logout/`, and static assets remain public so local operations and service checks keep working.

## Feature Flags

Feature flags let unfinished work live in the codebase without being visible in the stable local setup.
Flags for already shipped features default to enabled and act as module-level off switches.

Flags are created automatically after migrations and can be changed in Django admin:

```text
/admin/planner/featureflag/
```

Code can check a flag with:

```python
from planner.feature_flags import feature_enabled, feature_required

if feature_enabled("snapshots"):
    ...

@feature_required("snapshots")
def snapshots(request):
    ...
```

Templates receive a `feature_flags` dictionary:

```django
{% if feature_flags.snapshots %}
    ...
{% endif %}
```

Environment variables can override the admin value for a checkout:

```bash
LIF_FEATURE_SNAPSHOTS=1
LIF_FEATURE_MONEYMONEY_IMPORT=0
```

Use this for development or real-data checkouts where a feature must be forced on or off.

Currently shipped feature flags include:

- `analytics`
- `cash_goals`
- `depot_holdings`
- `debts`
- `real_estate`
- `income_investments`
- `retirement_plans`
- `equity_grants`
- `scenarios`
- `true_expenses`
- `child_milestones`
- `salary_changes`
- `imports`

Future or experimental flags currently include:

- `read_only_mode`
- `snapshots`
- `moneymoney_import`
- `ynab_import`
- `multi_language`
- `advanced_tax_model`
- `docker_deployment`
- `mobile_read_only`
- `mcp_server`

Local data such as `db.sqlite3` is intentionally ignored by Git.

## Languages

German is the first additional UI language. The shared navigation shell is translated, with page-specific translations intended to be added incrementally.
See [docs/I18N.md](docs/I18N.md) for the translation workflow.

## Local Production Operations

Create a timestamped SQLite backup:

```bash
pipenv run python manage.py backup_data
```

Or open the local Backup Center:

```text
/system/backups/
```

The Backup Center can create a manual backup and preview existing backup files by validating the SQLite schema and showing record counts. Restore is intentionally preview-only for now; use the manual rollback steps before overwriting real data.

Check production-readiness basics:

```bash
pipenv run python manage.py check_production
```

Run a smoke test after updates:

```bash
pipenv run python manage.py smoke_test
```

Run the local deployment flow:

```bash
pipenv run python manage.py deploy_local
```

That flow creates a pre-deploy SQLite backup, applies migrations, collects static files, runs production checks, and runs the smoke test.

**Static files gotcha:** with `DJANGO_DEBUG=0`, WhiteNoise serves CSS/JS through a
content-hashed manifest (`staticfiles/staticfiles.json`) instead of the raw files
under `planner/static/`. Any template, CSS, or JS change needs a fresh
`collectstatic` to regenerate that manifest, or the browser keeps loading the old
hashed file — this can look exactly like a browser cache issue, but a hard refresh
won't fix it. `manage.py deploy_local` and `scripts/update_and_run_8001.sh` already
run `collectstatic` on every deploy, so this only bites you if you `git pull` and
restart the server manually (e.g. a bare `manage.py runserver` in production mode)
without going through one of those.

For an always-on Mac mini deployment with VPN access and automatic updates on push, see:

```text
docs/MAC_MINI_DEPLOYMENT.md
```

Health status is available at:

```text
/health/
```

Real-data readiness is visible at:

```text
/system/
```

Use it before entering sensitive data. It checks login protection, debug mode, secret-key configuration, migrations, backups, feature flags, household foundation data, and data-quality status.

## Imports

The Import Center supports preview-first CSV imports for accounts and depot holdings. Each upload creates an import batch and dry-run table first. Clean batches can then be applied, which creates a backup before changing data.

Open:

```text
/imports/
```

Accounts CSV columns:

```csv
name,account_type,balance,currency,institution,as_of_date
Giro,cash,12000.00,EUR,DKB,2026-06-25
Depot,depot,50000.00,EUR,ING,2026-06-25
Mortgage,loan,300000.00,EUR,Sparkasse,2026-06-25
```

Valid account types are `cash`, `savings`, `depot`, `loan`, and `other`.

Depot holdings CSV columns:

```csv
account_name,name,isin,ticker,asset_class,quantity,latest_price,currency,as_of_date,payout_date
ING Depot,Vanguard FTSE All-World,IE00B3RBWM25,VGWL,ETF distributing,120.500000,118.42,EUR,2026-06-25,
ING Depot,German Government Bond 2031,IE0008UEVOE0,,Bond,20.000000,99.10,EUR,2026-06-25,2031-12-15
```

Depot holdings require an existing account with account type `depot`. Rows are matched by depot account plus ISIN when present, then ticker, then holding name.

Try the repeatable examples:

```bash
cp examples/accounts.example.csv /tmp/lif-accounts.csv
cp examples/accounts_update.example.csv /tmp/lif-accounts-update.csv
cp examples/depot_holdings.example.csv /tmp/lif-depot-holdings.csv
cp examples/depot_holdings_update.example.csv /tmp/lif-depot-holdings-update.csv
```

Upload `lif-accounts.csv` first to preview/create accounts, including your depot account. Then upload `lif-depot-holdings.csv` to preview/create holdings. The update examples preview updates against the same account and holding identifiers.

MoneyMoney import will use `py-money` as a local Mac-only connector. The adapter skeleton is present, but live import remains behind the `moneymoney_import` feature flag.

See:

```text
docs/MONEYMONEY_IMPORT.md
```

## Real Data Seeding

Keep real family, salary, account, and depot data outside Git. Use a private local folder plus the sanitized examples in `examples/`.

See:

```text
docs/REAL_DATA_SEEDING.md
```

For the recurring review loop that keeps real projections believable, see:

```text
docs/REAL_DATA_REVIEW_CADENCE.md
```

## Correctness Notes

Projection calculations are covered by regression tests for recurring rules, transfer rules, yearly rules, debt repayment, refinance assumptions, dated income investments, retirement income, equity grants, liquidity stress, long-term yearly aggregation, and audit pages.

The audit pages are part of the correctness strategy: every projected month or year should be explainable from opening balances and applied line items.

Future changes should also follow the guardrails in `ENGINEERING_GUARDRAILS.md`, especially around Decimal money handling, frontend DOM safety, projection auditability, analytics structure, and future German localization.
