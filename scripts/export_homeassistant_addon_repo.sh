#!/usr/bin/env sh
set -eu

DESTINATION="${1:-../home-assistant-addon}"
REMOTE_URL="${2:-git@github.com:lif-planner/home-assistant-addon.git}"
SOURCE_DIR="homeassistant-addon"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "Run this from the LiF repository root." >&2
    exit 1
fi

if [ -e "$DESTINATION" ]; then
    echo "Destination already exists: $DESTINATION" >&2
    exit 1
fi

mkdir -p "$DESTINATION"
cp -R "$SOURCE_DIR"/. "$DESTINATION"/

cd "$DESTINATION"
git init -b main
git add .
GIT_AUTHOR_NAME="LiF Maintainers" \
GIT_AUTHOR_EMAIL="yogitea@users.noreply.github.com" \
GIT_COMMITTER_NAME="LiF Maintainers" \
GIT_COMMITTER_EMAIL="yogitea@users.noreply.github.com" \
    git commit -m "Initial Home Assistant add-on repository"
git remote add origin "$REMOTE_URL"

cat <<EOF
Created standalone Home Assistant add-on repository at:
  $DESTINATION

Next steps:
  cd $DESTINATION
  git push -u origin main

Make sure the GitHub repository exists first:
  https://github.com/new

Recommended owner/name:
  lif-planner/home-assistant-addon
EOF
