#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/common.sh"

MOCK_SYSTEMD_USER=""

systemctl() {
    if [[ -n "$MOCK_SYSTEMD_USER" ]]; then
        printf '%s\n' "$MOCK_SYSTEMD_USER"
        return 0
    fi
    return 1
}

id() {
    [[ "$1" != "missing" ]]
}

getent() {
    printf '%s:x:1000:1000::/srv/%s:/bin/bash\n' "$2" "$2"
}

APP_USER="explicit"
SUDO_USER="sudo-user"
MOCK_SYSTEMD_USER="service-user"
resolve_app_identity "tg-bot" >/dev/null
[[ "$APP_USER" == "explicit" ]]
[[ "$APP_HOME" == "/srv/explicit" ]]

unset APP_USER APP_HOME
SUDO_USER="sudo-user"
MOCK_SYSTEMD_USER="service-user"
resolve_app_identity "tg-bot" >/dev/null
[[ "$APP_USER" == "service-user" ]]
[[ "$APP_HOME" == "/srv/service-user" ]]

unset APP_USER APP_HOME
SUDO_USER="sudo-user"
MOCK_SYSTEMD_USER=""
resolve_app_identity "tg-bot" >/dev/null
[[ "$APP_USER" == "sudo-user" ]]
[[ "$APP_HOME" == "/srv/sudo-user" ]]

APP_USER="root"
if resolve_app_identity "tg-bot" >/dev/null 2>&1; then
    echo "root must not be accepted as the application user" >&2
    exit 1
fi

APP_USER="missing"
if resolve_app_identity "tg-bot" >/dev/null 2>&1; then
    echo "a missing application user must not be accepted" >&2
    exit 1
fi

unset APP_USER APP_HOME
SUDO_USER="root"
if resolve_app_identity "tg-bot" >/dev/null 2>&1; then
    echo "an unresolved application user must fail" >&2
    exit 1
fi

validate_release_tag "v1.2.3"
validate_release_tag "v0.0.0"
for invalid_tag in "1.2.3" "v01.2.3" "v1.2" "v1.2.3-rc1" "main"; do
    if validate_release_tag "$invalid_tag"; then
        echo "invalid release tag was accepted: $invalid_tag" >&2
        exit 1
    fi
done

release_test_dir="$(mktemp -d)"
trap 'rm -rf "$release_test_dir"' EXIT
release_repo="$release_test_dir/repo"
release_remote="$release_test_dir/remote.git"
git init --quiet "$release_repo"
git init --quiet --bare "$release_remote"
git -C "$release_repo" config user.name "Release Test"
git -C "$release_repo" config user.email "release@example.com"
printf '1.2.3\n' > "$release_repo/VERSION"
git -C "$release_repo" add VERSION
git -C "$release_repo" commit --quiet -m "release"
git -C "$release_repo" tag -a v1.2.3 -m "Release v1.2.3"
git -C "$release_repo" remote add origin "$release_remote"
git -C "$release_repo" push --quiet origin HEAD --tags

runuser() {
    shift 3
    "$@"
}
APP_USER="release-test"
resolve_release_tag "$release_repo" "latest" >/dev/null
[[ "$RELEASE_TAG" == "v1.2.3" ]]
[[ "$RELEASE_COMMIT" == "$(git -C "$release_repo" rev-parse HEAD)" ]]

echo "Deployment identity tests passed."
