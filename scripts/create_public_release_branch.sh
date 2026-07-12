#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH_NAME="${PUBLIC_RELEASE_BRANCH:-public-release}"
TAG_NAME="${PUBLIC_RELEASE_TAG:-v1.0.0}"
COMMIT_MESSAGE="${PUBLIC_RELEASE_COMMIT_MESSAGE:-Initial public release (v1.0.0)}"
AUTHOR_NAME="${PUBLIC_AUTHOR_NAME:-LiF Maintainers}"
AUTHOR_EMAIL="${PUBLIC_AUTHOR_EMAIL:-yogitea@users.noreply.github.com}"
PIPENV_BIN="${PIPENV_BIN:-pipenv}"
TMP_ROOT="${TMPDIR:-/tmp}"
WORK_DIR="$(mktemp -d "$TMP_ROOT/lif-public-release.XXXXXX")"
EXPORT_TAR="$WORK_DIR/tracked-files.tar"
PUBLIC_REPO_DIR="$WORK_DIR/public-repo"

cd "$ROOT_DIR"

cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

fail() {
    echo "error: $*" >&2
    exit 1
}

if [[ "${CONFIRM_PUBLIC_RELEASE:-}" != "1" ]]; then
    fail "set CONFIRM_PUBLIC_RELEASE=1 to create the public release branch"
fi

if [[ -n "$(git status --porcelain)" ]]; then
    fail "working tree must be clean before creating a public release branch"
fi

if git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
    fail "branch '$BRANCH_NAME' already exists; delete or rename it before rerunning"
fi

if git show-ref --verify --quiet "refs/tags/$TAG_NAME"; then
    fail "tag '$TAG_NAME' already exists; delete or rename it before rerunning"
fi

if ! grep -q '"1.0.0"' lif/version.py; then
    fail "lif/version.py must default to 1.0.0 before the public release"
fi

if ! grep -q '^## \[1\.0\.0\]' CHANGELOG.md; then
    fail "CHANGELOG.md must contain a 1.0.0 section"
fi

if [[ -e docs/internal/FIXME.md ]] && ! git check-ignore -q docs/internal/FIXME.md; then
    fail "docs/internal/FIXME.md exists but is not ignored"
fi

echo "Running release checks..."
python3 scripts/scan_secrets.py
python3 scripts/scan_public_readiness.py
"$PIPENV_BIN" run python manage.py check

START_BRANCH="$(git branch --show-current)"

echo "Exporting clean tracked files..."
git archive --format=tar HEAD > "$EXPORT_TAR"
mkdir -p "$PUBLIC_REPO_DIR"
tar -xf "$EXPORT_TAR" -C "$PUBLIC_REPO_DIR"

echo "Checking private/generated files are absent..."
absent_paths=(
    ".env"
    ".run"
    "backups"
    "db.sqlite3"
    "docker/lif.env"
    "deploy/ansible/inventory.ini"
    "deploy/ansible/group_vars/lif/vars.yml"
    "deploy/ansible/group_vars/lif/vault.yml"
    "docs/internal/FIXME.md"
    "local_private"
    "data"
    "logs"
    "staticfiles"
)

for path in "${absent_paths[@]}"; do
    if [[ -e "$PUBLIC_REPO_DIR/$path" ]]; then
        fail "private or generated path is present in public export: $path"
    fi
done

if find "$PUBLIC_REPO_DIR" -type d -name "__pycache__" -print -quit | grep -q .; then
    fail "generated Python __pycache__ directory is present in public export"
fi

cd "$PUBLIC_REPO_DIR"
git init -q -b "$BRANCH_NAME"
git add -A

GIT_AUTHOR_NAME="$AUTHOR_NAME" \
GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
GIT_COMMITTER_NAME="$AUTHOR_NAME" \
GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
git commit -m "$COMMIT_MESSAGE"

git tag "$TAG_NAME"

cd "$ROOT_DIR"
git fetch "$PUBLIC_REPO_DIR" "refs/heads/$BRANCH_NAME:refs/heads/$BRANCH_NAME"
git fetch "$PUBLIC_REPO_DIR" "refs/tags/$TAG_NAME:refs/tags/$TAG_NAME"

cat <<EOF
Created public release branch '$BRANCH_NAME' and tag '$TAG_NAME' from a clean export.

Inspect the branch, then push to a new empty public repository:

  git remote add public git@github.com:lif-planner/lif.git
  git push public $BRANCH_NAME:main
  git push public $TAG_NAME

Return to private work:

  git switch $START_BRANCH
EOF
