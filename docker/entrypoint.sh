#!/usr/bin/env sh
set -eu

: "${DJANGO_DB_PATH:=/data/db.sqlite3}"
: "${DJANGO_STATIC_ROOT:=/data/staticfiles}"
: "${LIF_BACKUP_DIR:=/data/backups}"

export DJANGO_DB_PATH DJANGO_STATIC_ROOT LIF_BACKUP_DIR

mkdir -p "$(dirname "$DJANGO_DB_PATH")" "$DJANGO_STATIC_ROOT" "$LIF_BACKUP_DIR"

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [ "${LIF_RUN_PRODUCTION_CHECKS:-1}" = "1" ]; then
    python manage.py check_production
fi

exec "$@"
