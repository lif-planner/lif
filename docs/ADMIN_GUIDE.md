# Admin Guide

This is the entry point for installing, configuring, and operating a
self-hosted LiF instance. For how to *use* the app once it's running, see
[USER_GUIDE.md](USER_GUIDE.md) instead.

LiF is local-first and single-operator by design: one SQLite database per
deployment, no multi-tenant user accounts, no external services required.
Treat the SQLite database as your production data store.

## 1. Choosing a deployment method

| Method | Best for | Details |
| --- | --- | --- |
| Bare checkout (`pipenv`) | Local development, trying LiF out | [README.md](../README.md#local-setup) |
| Docker Compose / container | Proxmox LXC or any Linux host | [CONTAINER_DEPLOYMENT.md](CONTAINER_DEPLOYMENT.md) |
| Ansible-automated deploy | Repeatable, hands-off setup and updates | [ANSIBLE_DEPLOYMENT.md](ANSIBLE_DEPLOYMENT.md) |
| Always-on Mac mini (manual) | A dedicated Mac with LaunchAgent + auto-deploy on push | [MAC_MINI_DEPLOYMENT.md](MAC_MINI_DEPLOYMENT.md) |
| Home Assistant add-on | Home Assistant OS/Supervisor, experimental | [HOME_ASSISTANT_ADDON.md](HOME_ASSISTANT_ADDON.md) |

All methods run the same Django app against SQLite; they differ in how the
process is supervised and how updates get applied. Pick one and follow its
doc for the full step-by-step -- the sections below cover what's common to
all of them.

## 2. Core configuration

Every deployment is configured through environment variables (see
[README.md's Configuration section](../README.md#configuration) for the
full list). The ones that matter most for a real deployment:

- `DJANGO_SECRET_KEY` -- must be set to a real random value. `check_production`
  (below) will warn if it's still the insecure development default.
- `DJANGO_DEBUG` -- must be `0`/off for anything reachable outside your own
  machine.
- `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS` -- required once
  you're serving LiF from a real hostname, especially behind a reverse
  proxy or tunnel.
- `DJANGO_TRUST_PROXY_SSL_HEADER` -- only set this if you're behind a
  trusted TLS-terminating proxy/tunnel; it tells Django to trust the
  proxy's forwarded-scheme header.
- `DJANGO_DB_PATH` / `DJANGO_STATIC_ROOT` -- where the SQLite file and
  collected static assets live; the container/Ansible/Mac mini docs set
  sensible defaults for their own layout.
- `LIF_VERSION` / `LIF_GIT_COMMIT` -- surfaced in the UI footer and
  `/health/`; the Ansible playbook and Docker build populate these
  automatically from the checkout.

## 3. Requiring login

By default `LIF_REQUIRE_LOGIN` is off so local/demo checkouts stay
frictionless. **Any checkout holding real household data should turn this
on:**

```bash
export LIF_REQUIRE_LOGIN=1
pipenv run python manage.py createsuperuser
```

With it enabled, all planner pages require login. `/health/`, `/login/`,
`/logout/`, and static assets stay public so monitoring and the login page
itself keep working. Since LiF is single-operator, one Django user account
is normal -- there's no per-user data separation model.

## 4. Feature flags

Feature flags gate work that's still in progress so it can live in the
codebase without being visible in a stable setup. They're managed at
`/admin/planner/featureflag/` and can be forced per-checkout with
`LIF_FEATURE_<NAME>=1` or `=0` (e.g. `LIF_FEATURE_SNAPSHOTS=1`). See
[README.md's Feature Flags section](../README.md#feature-flags) for the
current list of shipped vs. experimental flags.

`read_only_mode` is worth knowing about specifically: turn it on before
risky maintenance (an update, a bulk import) to keep pages readable while
blocking writes, without locking yourself out of Django admin.

## 5. Backups and recovery

```bash
pipenv run python manage.py backup_data
```

creates a timestamped SQLite backup under `backups/` (override with
`LIF_BACKUP_DIR`). The in-app **Backup Center** (`/system/backups/`) can
also trigger a manual backup and preview existing backup files (record
counts, schema validation) -- restore itself is preview-only there; use the
manual rollback steps in [DEPLOYMENT.md](../DEPLOYMENT.md#rollback) (or the
container/Ansible/Mac-mini doc's own rollback section) to actually restore
a backup file.

Always take a backup before: an update, a bulk import, or switching a
household from demo to real data (`real_data_start` prompts for this).

## 6. Health, updates, and production readiness

- `/health/` -- database connectivity, migration state, static file
  discovery, app version, and Git commit. Public even when
  `LIF_REQUIRE_LOGIN` is on, so it works as a monitoring endpoint.
- `/system/` -- a fuller **System status** page: login protection, debug
  mode, secret-key configuration, migrations, backups, feature flags, and
  household foundation/data-quality status.
- `pipenv run python manage.py check_production` -- a command-line version
  of the same checks (debug mode, default secret key, missing backups
  directory, pending migrations, missing feature flag rows).
- `pipenv run python manage.py deploy_local` -- the update flow: pre-deploy
  backup, `migrate`, `collectstatic`, `check_production`, and a smoke test,
  all in one command. See [DEPLOYMENT.md](../DEPLOYMENT.md) for the full
  update/rollback flow, or the deployment-method-specific doc if you're
  using Docker/Ansible/Mac mini (they each wrap this differently).

**Static files gotcha:** with `DJANGO_DEBUG=0`, static assets are served
through a content-hashed manifest. A template/CSS/JS change needs a fresh
`collectstatic` (already part of `deploy_local`) or the browser keeps
loading the old file -- this can look exactly like a caching issue but a
hard refresh won't fix it.

## 7. Secrets hygiene

Enable the local pre-commit hook once per checkout so staged files get
scanned for accidental secrets before every commit:

```bash
git config core.hooksPath .githooks
```

You can also scan all tracked files manually with
`python3 scripts/scan_secrets.py`.

## 8. Real data safety

Keep real family/salary/account/depot data out of Git entirely -- see
[REAL_DATA_SEEDING.md](REAL_DATA_SEEDING.md) for what belongs where and how
first-time real-data setup works, and
[REAL_DATA_REVIEW_CADENCE.md](REAL_DATA_REVIEW_CADENCE.md) for a suggested
monthly/quarterly/yearly review rhythm once real data is in place.

## 9. Imports

The Import Center (`/imports/`) supports preview-first CSV imports for
accounts and depot holdings. MoneyMoney import is Mac-only and still behind
the `moneymoney_import` feature flag -- see
[MONEYMONEY_IMPORT.md](MONEYMONEY_IMPORT.md).

## 10. Internationalization

If you're customizing or translating templates, see
[I18N.md](I18N.md) for the string-marking convention and how to regenerate
and update the German translation catalog.

## Further reading

- [../README.md](../README.md) -- full local setup, configuration
  reference, and feature flag list.
- [../DEPLOYMENT.md](../DEPLOYMENT.md) -- update flow, manual backup, health
  check, read-only mode, rollback, production readiness.
- [../ENGINEERING_GUARDRAILS.md](../ENGINEERING_GUARDRAILS.md) -- if you're
  modifying LiF yourself, the conventions to follow (Decimal money handling,
  projection auditability, etc.).
