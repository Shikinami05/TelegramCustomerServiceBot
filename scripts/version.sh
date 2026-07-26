#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION_FILE="$PROJECT_DIR/VERSION"

source "$SCRIPT_DIR/common.sh"

usage() {
    cat <<'EOF'
Usage: bash scripts/version.sh [--short]

Shows the declared release version, Git ref, commit, and checkout state.
EOF
}

mode="full"
case "${1:-}" in
    "")
        ;;
    --short)
        mode="short"
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if [[ ! -f "$VERSION_FILE" || ! -d "$PROJECT_DIR/.git" ]]; then
    echo "VERSION or Git metadata is missing in $PROJECT_DIR." >&2
    exit 1
fi

declared_version="$(tr -d '[:space:]' < "$VERSION_FILE")"
if ! validate_release_tag "v${declared_version}"; then
    echo "VERSION must contain a stable semantic version such as 1.2.3." >&2
    exit 1
fi

commit="$(git -C "$PROJECT_DIR" rev-parse HEAD)"
short_commit="$(git -C "$PROJECT_DIR" rev-parse --short=12 HEAD)"
branch="$(git -C "$PROJECT_DIR" symbolic-ref --quiet --short HEAD || true)"
declared_tag="v${declared_version}"
release_commit="$(
    git -C "$PROJECT_DIR" rev-parse --quiet --verify \
        "refs/tags/${declared_tag}^{commit}" 2>/dev/null || true
)"
state="clean"
if [[ -n "$(git -C "$PROJECT_DIR" status --porcelain)" ]]; then
    state="modified"
fi

display_version="v${declared_version}+${short_commit}"
if [[ "$release_commit" == "$commit" ]]; then
    display_version="$declared_tag"
fi

if [[ "$mode" == "short" ]]; then
    printf '%s\n' "$display_version"
    exit 0
fi

printf 'Version: %s\n' "$display_version"
printf 'Declared: v%s\n' "$declared_version"
printf 'Git ref: %s\n' "${branch:-detached}"
printf 'Commit: %s\n' "$commit"
printf 'Checkout: %s\n' "$state"
