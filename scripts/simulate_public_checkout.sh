#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPENV_BIN="${PIPENV_BIN:-pipenv}"
KEEP_DIR="${KEEP_PUBLIC_SIMULATION_DIR:-0}"
TMP_ROOT="${TMPDIR:-/tmp}"
WORK_DIR="$(mktemp -d "$TMP_ROOT/lif-public-checkout.XXXXXX")"
EXPORT_TAR="$WORK_DIR/tracked-files.tar"
CHECKOUT_DIR="$WORK_DIR/checkout"

export LC_ALL=C
export LANG=C

cleanup() {
    if [[ "$KEEP_DIR" != "1" ]]; then
        rm -rf "$WORK_DIR"
    else
        echo "Kept simulation directory: $WORK_DIR"
    fi
}
trap cleanup EXIT

fail() {
    echo "error: $*" >&2
    exit 1
}

cd "$ROOT_DIR"

if [[ -n "$(git status --porcelain)" ]]; then
    fail "working tree must be clean before simulating a public checkout"
fi

echo "Exporting tracked files to a clean checkout..."
git archive --format=tar HEAD > "$EXPORT_TAR"
mkdir -p "$CHECKOUT_DIR"
tar -xf "$EXPORT_TAR" -C "$CHECKOUT_DIR"

cd "$CHECKOUT_DIR"

git init -q
git add -A

echo "Checking ignored/private files are absent..."
absent_paths=(
    ".env"
    "db.sqlite3"
    "docker/lif.env"
    "deploy/ansible/inventory.ini"
    "deploy/ansible/group_vars/lif/vars.yml"
    "deploy/ansible/group_vars/lif/vault.yml"
    "docs/internal/FIXME.md"
    "local_private"
    "data"
)

for path in "${absent_paths[@]}"; do
    if [[ -e "$path" ]]; then
        fail "private or generated path is present in clean checkout: $path"
    fi
done

echo "Running public scans..."
python3 scripts/scan_secrets.py
python3 scripts/scan_public_readiness.py

export DJANGO_DB_PATH="$WORK_DIR/db.sqlite3"
export DJANGO_STATIC_ROOT="$WORK_DIR/staticfiles"
export LIF_BACKUP_DIR="$WORK_DIR/backups"
export DJANGO_SECRET_KEY="public-simulation-only"
export DJANGO_DEBUG="1"
export LIF_REQUIRE_LOGIN="0"
export PIPENV_VENV_IN_PROJECT="1"
export PIPENV_CACHE_DIR="$WORK_DIR/pipenv-cache"
export VIRTUALENV_APP_DATA="$WORK_DIR/virtualenv-app-data"
export WORKON_HOME="$WORK_DIR/virtualenvs"

echo "Installing locked dependencies..."
"$PIPENV_BIN" install --deploy

echo "Applying migrations..."
"$PIPENV_BIN" run python manage.py migrate --noinput

echo "Seeding demo data..."
"$PIPENV_BIN" run python manage.py seed_demo

echo "Running Django checks and smoke test..."
"$PIPENV_BIN" run python manage.py check
"$PIPENV_BIN" run python manage.py smoke_test

echo "Public checkout simulation passed."
