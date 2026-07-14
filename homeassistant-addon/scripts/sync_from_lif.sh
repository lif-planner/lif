#!/usr/bin/env sh
set -eu

LIF_REPO="${1:-../LiF}"
SOURCE="${LIF_REPO}/homeassistant-addon"

if [ ! -d "$SOURCE" ]; then
    echo "Could not find staged add-on source: $SOURCE" >&2
    exit 1
fi

cp -R "$SOURCE"/. .

echo "Synced Home Assistant add-on files from $SOURCE"
echo "Run scripts/validate.sh before committing."
