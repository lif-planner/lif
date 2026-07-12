#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
INVENTORY="${LIF_ANSIBLE_INVENTORY:-$ROOT_DIR/deploy/ansible/inventory.ini}"
PLAYBOOK="${LIF_ANSIBLE_PLAYBOOK:-$ROOT_DIR/deploy/ansible/lif.yml}"

cd "$ROOT_DIR"
exec ansible-playbook -i "$INVENTORY" "$PLAYBOOK" "$@"
