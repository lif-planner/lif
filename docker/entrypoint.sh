#!/usr/bin/env sh
set -eu

# Home Assistant add-on support. The published add-on declares an `image:` in
# its config.yaml, so Supervisor pulls this generic image and the add-on's own
# run.sh never executes. Supervisor always mounts /data with options.json
# inside an add-on container, so its presence is the reliable signal that this
# container needs the add-on environment established here.
ADDON_OPTIONS=/data/options.json

addon_option() {
    python - "$ADDON_OPTIONS" "$1" "$2" <<'PY'
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

LIF_ADDON_SEED_DEMO=0
LIF_ADDON_SEED_DEMO_ON_START=0

if [ -f "$ADDON_OPTIONS" ]; then
    ENV_FILE=/data/lif.env
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

    # run.sh historically used /data/lif.sqlite3; earlier image-based installs
    # created /data/db.sqlite3 via the default below. Prefer whichever already
    # exists so neither install path loses its database on update.
    if [ -z "${DJANGO_DB_PATH:-}" ]; then
        if [ -f /data/lif.sqlite3 ]; then
            DJANGO_DB_PATH=/data/lif.sqlite3
        elif [ -f /data/db.sqlite3 ]; then
            DJANGO_DB_PATH=/data/db.sqlite3
        else
            DJANGO_DB_PATH=/data/lif.sqlite3
        fi
        export DJANGO_DB_PATH
    fi

    export DJANGO_DEBUG="${DJANGO_DEBUG:-0}"
    export LIF_HOME_ASSISTANT_ADDON=1
    export DJANGO_TRUST_PROXY_SSL_HEADER="${DJANGO_TRUST_PROXY_SSL_HEADER:-1}"
    export LIF_REQUIRE_LOGIN="$(addon_option login_required 0)"
    export DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-$(addon_option allowed_hosts "*")}"
    export LIF_RUN_PRODUCTION_CHECKS="${LIF_RUN_PRODUCTION_CHECKS:-0}"
    LIF_ADDON_SEED_DEMO="$(addon_option demo_mode 1)"
    LIF_ADDON_SEED_DEMO_ON_START="$(addon_option seed_demo_on_start 0)"
fi

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

if [ "$LIF_ADDON_SEED_DEMO" = "1" ] || [ "$LIF_ADDON_SEED_DEMO_ON_START" = "1" ]; then
    python manage.py seed_demo_if_needed --marker-file /data/.demo_seeded
fi

exec "$@"
