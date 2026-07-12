#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/.run/lif-8001.pid"
LOG_FILE="$ROOT_DIR/logs/lif-8001.log"
HOST="${LIF_RUN_HOST:-0.0.0.0}"
PORT="${LIF_RUN_PORT:-8001}"
PIPENV_BIN="${PIPENV_BIN:-pipenv}"
LIF_ALLOWED_HOST_PREFIXES="${LIF_ALLOWED_HOST_PREFIXES:-}"

cd "$ROOT_DIR"
mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

append_allowed_host() {
    local candidate="$1"
    if [[ -z "$candidate" ]]; then
        return
    fi
    case ",$DJANGO_ALLOWED_HOSTS," in
        *",$candidate,"*) ;;
        *) DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS},${candidate}" ;;
    esac
}

DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-127.0.0.1,localhost}"
if [[ "$HOST" == "0.0.0.0" ]]; then
    append_allowed_host "$(hostname 2>/dev/null || true)"
    short_hostname="$(hostname -s 2>/dev/null || true)"
    append_allowed_host "$short_hostname"
    if [[ -n "$short_hostname" ]]; then
        append_allowed_host "${short_hostname}.local"
    fi
    IFS=',' read -r -a allowed_host_prefixes <<< "$LIF_ALLOWED_HOST_PREFIXES"
    for prefix in "${allowed_host_prefixes[@]}"; do
        prefix="${prefix//[[:space:]]/}"
        if [[ -z "$prefix" ]]; then
            continue
        fi
        for octet in {1..254}; do
            append_allowed_host "${prefix}${octet}"
        done
    done
    if command -v ifconfig >/dev/null 2>&1; then
        while IFS= read -r ip_address; do
            append_allowed_host "$ip_address"
        done < <(ifconfig | awk '/inet / {print $2}' | grep -Ev '^127\.|^0\.0\.0\.0$' || true)
    fi
fi
export DJANGO_ALLOWED_HOSTS

if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE")"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping LiF server from $PID_FILE (pid $OLD_PID)..."
        kill "$OLD_PID"
        for _ in {1..30}; do
            if ! kill -0 "$OLD_PID" 2>/dev/null; then
                break
            fi
            sleep 0.2
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "Server did not stop gracefully; sending SIGKILL..."
            kill -9 "$OLD_PID"
        fi
    fi
    rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
    echo "Port $PORT is already in use by a process not started by this script."
    echo "Stop that process or run with a different port, for example: LIF_RUN_PORT=8002 $0"
    exit 1
fi

echo "Allowed hosts: $DJANGO_ALLOWED_HOSTS"

echo "Pulling latest code..."
git pull --ff-only

echo "Installing locked dependencies..."
"$PIPENV_BIN" sync

echo "Applying migrations..."
"$PIPENV_BIN" run python manage.py migrate

echo "Collecting static files..."
# Required for DEBUG=0 (WhiteNoise serves hashed files via the manifest). Without
# this, a code pull updates the templates/source CSS but keeps serving the stale
# collected app.<oldhash>.css, breaking the layout.
"$PIPENV_BIN" run python manage.py collectstatic --noinput

echo "Starting LiF on http://$HOST:$PORT/ ..."
nohup "$PIPENV_BIN" run python manage.py runserver "$HOST:$PORT" > "$LOG_FILE" 2>&1 &
SERVER_PID="$!"
echo "$SERVER_PID" > "$PID_FILE"

sleep 1
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "LiF did not start. Last log lines:"
    tail -40 "$LOG_FILE" || true
    rm -f "$PID_FILE"
    exit 1
fi

echo "LiF is running with pid $SERVER_PID"
echo "Log: $LOG_FILE"
