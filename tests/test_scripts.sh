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

echo "Deployment identity tests passed."
