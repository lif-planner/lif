# Local Deployment

LiF is local-first. Treat the SQLite database as the production data store unless a later deployment changes that explicitly.

For the Mac mini setup with LaunchAgent and GitHub Actions self-hosted runner, see `docs/MAC_MINI_DEPLOYMENT.md`.

## Update Flow

From the real-data checkout:

```bash
git pull
pipenv install
pipenv run python manage.py deploy_local
```

`deploy_local` runs:

- `backup_data --label pre-deploy`
- `migrate`
- `collectstatic --noinput`
- `check_production`
- `smoke_test`

Static files are served by WhiteNoise from `STATIC_ROOT`, which defaults to `staticfiles/` in the checkout.
Override it with `DJANGO_STATIC_ROOT=/path/to/staticfiles` if the deployment checkout should keep collected assets elsewhere.

## Manual Backup

Create a backup before risky changes or imports:

```bash
pipenv run python manage.py backup_data
```

Backups default to `backups/`. Override with:

```bash
LIF_BACKUP_DIR=/path/to/backups pipenv run python manage.py backup_data
```

## Health Check

Open:

```text
/health/
```

The endpoint checks database connectivity, migration state, static file discovery, app version, and Git commit.

## Read-Only Mode

Use the `read_only_mode` feature flag before risky maintenance. It keeps pages readable and blocks planner writes while leaving Django admin available so the flag can be turned off again.

You can also force it for a checkout:

```bash
LIF_FEATURE_READ_ONLY_MODE=1 pipenv run python manage.py runserver 127.0.0.1:8000
```

## Rollback

If an update breaks the app:

1. Stop the running app.
2. Check the latest backup in `backups/`.
3. Return code to the previous commit:

```bash
git log --oneline -5
git checkout <known-good-commit>
```

4. Restore the database backup:

```bash
cp backups/<backup-file>.sqlite3 db.sqlite3
```

5. Run:

```bash
pipenv install
pipenv run python manage.py check
pipenv run python manage.py smoke_test
```

## Production Readiness Check

Run:

```bash
pipenv run python manage.py check_production
```

It reports warnings for debug mode, default secret key, missing backups directory, pending migrations, and missing feature flag rows.
