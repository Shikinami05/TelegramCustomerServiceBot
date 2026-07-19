#!/usr/bin/env bash

resolve_app_identity() {
    local service_name="$1"
    local candidate="${APP_USER:-}"
    local source_name="APP_USER"
    local service_user=""

    if [[ -z "$candidate" ]] && command -v systemctl >/dev/null 2>&1; then
        service_user="$(systemctl show "$service_name" --property=User --value 2>/dev/null || true)"
        if [[ -n "$service_user" ]]; then
            candidate="$service_user"
            source_name="systemd service $service_name"
        fi
    fi

    if [[ -z "$candidate" && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        candidate="$SUDO_USER"
        source_name="SUDO_USER"
    fi

    if [[ -z "$candidate" ]]; then
        echo "Unable to determine the application user." >&2
        echo "Run from the target VPS user with sudo, or set it explicitly:" >&2
        echo "  sudo APP_USER=ubuntu bash scripts/install.sh" >&2
        return 1
    fi
    if [[ "$candidate" == "root" ]]; then
        echo "Refusing to run the Bot service as root. Set APP_USER to a non-root user." >&2
        return 1
    fi
    if ! id "$candidate" >/dev/null 2>&1; then
        echo "Linux user does not exist: $candidate" >&2
        return 1
    fi

    APP_HOME="$(getent passwd "$candidate" | cut -d: -f6)"
    if [[ -z "$APP_HOME" || "$APP_HOME" != /* ]]; then
        echo "Unable to determine a valid home directory for $candidate." >&2
        return 1
    fi

    APP_USER="$candidate"
    export APP_USER APP_HOME
    echo "Using application user $APP_USER ($source_name), home $APP_HOME"
}
