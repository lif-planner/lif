#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: scripts/set_version.sh X.Y.Z" >&2
    exit 1
fi

VERSION="$1"

python3 - "$VERSION" <<'PY'
import re
import sys
from pathlib import Path

version = sys.argv[1]
if not re.fullmatch(r"\d+\.\d+\.\d+([.-][A-Za-z0-9]+)?", version):
    raise SystemExit(f"Version does not look like an add-on release: {version}")

path = Path("lif/config.yaml")
text = path.read_text()
text = re.sub(r'^version: ".*"$', f'version: "{version}"', text, flags=re.MULTILINE)
path.write_text(text)
PY

echo "Updated lif/config.yaml to version $VERSION"
