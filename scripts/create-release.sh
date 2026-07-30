#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

source "$SCRIPT_DIR/common.sh"

usage() {
    cat <<'EOF'
Usage: bash scripts/create-release.sh v1.2.3

Validates main, runs the test suite, creates an annotated tag, and pushes it.
The release workflow publishes the matching GitHub Release.
EOF
}

release_tag="${1:-}"
if [[ "$release_tag" == "-h" || "$release_tag" == "--help" ]]; then
    usage
    exit 0
fi
if ! validate_release_tag "$release_tag"; then
    usage >&2
    exit 2
fi
if [[ ! -d "$PROJECT_DIR/.git" ]]; then
    echo "The project must be a Git checkout." >&2
    exit 1
fi
if [[ -n "$(git -C "$PROJECT_DIR" status --porcelain)" ]]; then
    echo "The Git checkout has local changes; release aborted." >&2
    exit 1
fi
if [[ "$(git -C "$PROJECT_DIR" branch --show-current)" != "main" ]]; then
    echo "Releases must be created from main." >&2
    exit 1
fi

declared_version="$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")"
if [[ "$release_tag" != "v${declared_version}" ]]; then
    echo "Tag $release_tag does not match VERSION ($declared_version)." >&2
    exit 1
fi

git -C "$PROJECT_DIR" fetch --tags origin main
if [[ "$(git -C "$PROJECT_DIR" rev-parse HEAD)" \
    != "$(git -C "$PROJECT_DIR" rev-parse origin/main)" ]]; then
    echo "Local main must exactly match origin/main." >&2
    exit 1
fi
if git -C "$PROJECT_DIR" show-ref --tags --verify \
    --quiet "refs/tags/$release_tag"; then
    echo "Tag already exists: $release_tag" >&2
    exit 1
fi

cd "$PROJECT_DIR"
"$PYTHON_BIN" -m py_compile \
    app.py scripts/manage_webhook.py scripts/manage_backup.py \
    scripts/manage_turnstile.py
"$PYTHON_BIN" -m unittest discover -s tests -v
bash -n scripts/*.sh
bash tests/test_scripts.sh

git tag -a "$release_tag" -m "Release $release_tag"
git push origin "$release_tag"
echo "Tag $release_tag pushed. GitHub Release creation is handled by Actions."
