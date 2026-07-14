#!/usr/bin/env sh
set -eu

IMAGE="${1:-lif-smoke:local}"
PORT="${LIF_SMOKE_PORT:-18080}"
DATA_DIR="${LIF_SMOKE_DATA_DIR:-}"

cleanup() {
    if [ -n "${CONTAINER_ID:-}" ]; then
        docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true
    fi
    if [ -n "${DATA_DIR_CREATED:-}" ] && [ -d "$DATA_DIR_CREATED" ]; then
        rm -rf "$DATA_DIR_CREATED"
    fi
}
trap cleanup EXIT INT TERM

if [ -z "$DATA_DIR" ]; then
    DATA_DIR_CREATED="$(mktemp -d)"
    DATA_DIR="$DATA_DIR_CREATED"
fi

CONTAINER_ID="$(
    docker run -d \
        -p "127.0.0.1:${PORT}:8000" \
        -v "${DATA_DIR}:/data" \
        -e DJANGO_SECRET_KEY="container-smoke-test-secret" \
        -e DJANGO_DEBUG=0 \
        -e DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost" \
        -e LIF_REQUIRE_LOGIN=0 \
        -e LIF_RUN_PRODUCTION_CHECKS=0 \
        "$IMAGE"
)"

for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${PORT}/health/" >/tmp/lif-smoke-health.json; then
        cat /tmp/lif-smoke-health.json
        echo
        exit 0
    fi
    sleep 1
done

docker logs "$CONTAINER_ID"
echo "LiF container smoke test failed: /health/ did not become ready." >&2
exit 1
