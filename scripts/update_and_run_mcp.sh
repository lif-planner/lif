#!/usr/bin/env bash
set -euo pipefail

# Updates the checkout and starts the read-only LiF MCP server.
#
# Unlike the web launcher, the MCP server speaks JSON-RPC over stdio: an MCP
# client (e.g. Claude Desktop) spawns this script and owns its stdin/stdout.
# So the server must run in the FOREGROUND and stdout must carry ONLY the
# protocol -- every setup message below is sent to stderr, and we exec the
# server so stdio passes straight through.
#
# Env toggles:
#   LIF_FEATURE_MCP_SERVER  default 1 -- running this launcher opts in to MCP.
#   LIF_MCP_SKIP_UPDATE     set to 1 to skip git pull / sync / migrate (handy
#                           when a client respawns the server frequently).
#   PIPENV_BIN              pipenv executable (default: pipenv).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPENV_BIN="${PIPENV_BIN:-pipenv}"
export LIF_FEATURE_MCP_SERVER="${LIF_FEATURE_MCP_SERVER:-1}"

cd "$ROOT_DIR"

log() { echo "$@" >&2; }

if [[ "${LIF_MCP_SKIP_UPDATE:-0}" != "1" ]]; then
    log "Pulling latest code..."
    git pull --ff-only >&2

    log "Installing locked dependencies..."
    "$PIPENV_BIN" sync >&2

    log "Ensuring the MCP package is installed..."
    "$PIPENV_BIN" run python -c "import mcp" >/dev/null 2>&1 || "$PIPENV_BIN" install mcp >&2

    log "Applying migrations..."
    "$PIPENV_BIN" run python manage.py migrate >&2
fi

log "Starting LiF MCP server (read-only, stdio). Connect an MCP client to this command."
exec "$PIPENV_BIN" run python manage.py run_mcp_server
