#!/usr/bin/env sh
set -eu

# All add-on environment handling lives in docker/entrypoint.sh, which detects
# the Home Assistant add-on container via /data/options.json. Keeping this a
# thin delegator means locally built add-ons and the published image
# (config.yaml `image:` pulls the generic container built from the repo-root
# Dockerfile) run exactly the same startup logic.
exec /app/docker/entrypoint.sh gunicorn lif.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --threads 4 \
    --timeout 120
