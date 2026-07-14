#!/usr/bin/env sh
set -eu

required_files="
repository.yaml
README.md
lif/config.yaml
lif/DOCS.md
lif/README.md
lif/CHANGELOG.md
lif/run.sh
lif/icon.png
lif/logo.png
lif/translations/en.yaml
lif/translations/de.yaml
"

for path in $required_files; do
    if [ ! -f "$path" ]; then
        echo "Missing required file: $path" >&2
        exit 1
    fi
done

ruby -e "require 'yaml'; Dir['**/*.yaml'].each { |path| YAML.load_file(path) }"

sh -n lif/run.sh

python3 - <<'PY'
from pathlib import Path

config = Path("lif/config.yaml").read_text()
required = [
    'image: "ghcr.io/lif-planner/lif"',
    'ingress: true',
    'ingress_port: 8000',
    'allowed_hosts:\n    - "*"',
    'backup: cold',
]
missing = [item for item in required if item not in config]
if missing:
    raise SystemExit("Missing required config entries: " + ", ".join(missing))
PY

echo "Home Assistant add-on repository validation passed."
