#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
INVENTORY="${LIF_ANSIBLE_INVENTORY:-$ROOT_DIR/deploy/ansible/inventory.ini}"
PLAYBOOK="${LIF_ANSIBLE_SEED_RESET_PLAYBOOK:-$ROOT_DIR/deploy/ansible/reset_demo_seed.yml}"

cd "$ROOT_DIR"
exec ansible-playbook -i "$INVENTORY" "$PLAYBOOK" "$@"
