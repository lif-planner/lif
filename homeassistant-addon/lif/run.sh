#!/usr/bin/env sh
set -eu

CONFIG_PATH=/data/options.json
ENV_FILE=/data/lif.env
DB_PATH=/data/lif.sqlite3
STATIC_ROOT=/data/staticfiles
BACKUP_DIR=/data/backups

mkdir -p /data "$STATIC_ROOT" "$BACKUP_DIR"

if [ ! -f "$ENV_FILE" ]; then
    SECRET_KEY="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(64))
PY
)"
    {
        echo "DJANGO_SECRET_KEY=$SECRET_KEY"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi

set -a
. "$ENV_FILE"
set +a

option() {
    python - "$CONFIG_PATH" "$1" "$2" <<'PY'
import json
import sys

path, key, default = sys.argv[1:4]
try:
    with open(path, "r", encoding="utf-8") as handle:
        options = json.load(handle)
except FileNotFoundError:
    options = {}

value = options.get(key, default)
if isinstance(value, bool):
    print("1" if value else "0")
elif isinstance(value, list):
    print(",".join(str(item) for item in value if str(item).strip()))
else:
    print(value)
PY
}

export DJANGO_DEBUG=0
export DJANGO_DB_PATH="$DB_PATH"
export DJANGO_STATIC_ROOT="$STATIC_ROOT"
export LIF_BACKUP_DIR="$BACKUP_DIR"
export LIF_HOME_ASSISTANT_ADDON=1
export LIF_REQUIRE_LOGIN="$(option login_required 0)"
export DJANGO_ALLOWED_HOSTS="$(option allowed_hosts "*")"
export DJANGO_TRUST_PROXY_SSL_HEADER=1
export LIF_RUN_PRODUCTION_CHECKS=0

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [ "$(option demo_mode 1)" = "1" ]; then
    python manage.py seed_demo_if_needed --marker-file /data/.demo_seeded
fi

if [ "$(option seed_demo_on_start 0)" = "1" ]; then
    python manage.py seed_demo_if_needed --marker-file /data/.demo_seeded
fi

exec gunicorn lif.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --threads 4 \
    --timeout 120
