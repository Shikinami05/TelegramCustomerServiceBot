#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-tg-bot}"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"

usage() {
    cat <<'EOF'
Usage: sudo tg-bot COMMAND [ARGUMENTS]

Commands:
  update [latest|v1.2.3]       Update to the latest or selected release
  backup [KEEP]                Create a manual SQLite backup (default: keep 10)
  status                       Show service and health status
  restart                      Restart the service and check health
  logs [LINES]                 Show recent service logs (default: 100)
  version                      Show the deployed version
  webhook                      Show Telegram webhook status
  turnstile status|enable|disable
                               Manage Cloudflare Turnstile
  configure DOMAIN EMAIL [PORT] Configure HTTPS and webhook (443 or 8443)
  help                         Show this help
EOF
}

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this command with sudo: sudo tg-bot ${*:-help}" >&2
    exit 1
fi

source "$SCRIPT_DIR/common.sh"

wait_for_health() {
    for _ in {1..20}; do
        if curl --fail --silent http://127.0.0.1:9000/healthz >/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

restart_and_check() {
    systemctl restart "$SERVICE_NAME"
    if ! wait_for_health; then
        journalctl -u "$SERVICE_NAME" -n 80 --no-pager >&2 || true
        return 1
    fi
    curl --fail --silent --show-error http://127.0.0.1:9000/healthz
    echo
}

command_name="${1:-help}"
if [[ $# -gt 0 ]]; then
    shift
fi

case "$command_name" in
    update)
        if [[ $# -gt 1 ]]; then
            usage >&2
            exit 2
        fi
        version="${1:-latest}"
        exec bash "$SCRIPT_DIR/update.sh" --version "$version"
        ;;
    backup)
        if [[ $# -gt 1 ]]; then
            usage >&2
            exit 2
        fi
        keep="${1:-10}"
        if [[ ! "$keep" =~ ^[1-9][0-9]*$ ]]; then
            echo "KEEP must be a positive integer." >&2
            exit 2
        fi
        resolve_app_identity "$SERVICE_NAME"
        if [[ ! -x "$PYTHON_BIN" || ! -f "$PROJECT_DIR/bot.db" ]]; then
            echo "The installed virtual environment or database is missing." >&2
            exit 1
        fi
        backup_dir="$PROJECT_DIR/backups/manual"
        install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$backup_dir"
        output="$backup_dir/manual-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"
        runuser -u "$APP_USER" -- "$PYTHON_BIN" \
            "$SCRIPT_DIR/manage_backup.py" create \
            --database "$PROJECT_DIR/bot.db" \
            --output "$output" \
            --keep "$keep" \
            --kind manual
        ;;
    status)
        if [[ $# -ne 0 ]]; then
            usage >&2
            exit 2
        fi
        systemctl status "$SERVICE_NAME" --no-pager --lines=20 || true
        echo
        curl --fail --silent --show-error http://127.0.0.1:9000/healthz
        echo
        ;;
    restart)
        if [[ $# -ne 0 ]]; then
            usage >&2
            exit 2
        fi
        restart_and_check
        ;;
    logs)
        if [[ $# -gt 1 ]]; then
            usage >&2
            exit 2
        fi
        lines="${1:-100}"
        if [[ ! "$lines" =~ ^[1-9][0-9]*$ ]]; then
            echo "LINES must be a positive integer." >&2
            exit 2
        fi
        exec journalctl -u "$SERVICE_NAME" -n "$lines" --no-pager
        ;;
    version)
        if [[ $# -ne 0 ]]; then
            usage >&2
            exit 2
        fi
        resolve_app_identity "$SERVICE_NAME"
        exec runuser -u "$APP_USER" -- bash "$SCRIPT_DIR/version.sh"
        ;;
    webhook)
        if [[ $# -ne 0 ]]; then
            usage >&2
            exit 2
        fi
        resolve_app_identity "$SERVICE_NAME"
        exec runuser -u "$APP_USER" -- \
            "$PYTHON_BIN" "$SCRIPT_DIR/manage_webhook.py" --info
        ;;
    turnstile)
        if [[ $# -ne 1 ]] \
            || [[ "$1" != "status" && "$1" != "enable" && "$1" != "disable" ]]; then
            usage >&2
            exit 2
        fi
        action="$1"
        resolve_app_identity "$SERVICE_NAME"
        env_file="$PROJECT_DIR/.env"
        helper="$SCRIPT_DIR/manage_turnstile.py"
        if [[ ! -x "$PYTHON_BIN" || ! -f "$helper" || ! -f "$env_file" ]]; then
            echo "The installed environment or Turnstile helper is missing." >&2
            exit 1
        fi
        if [[ "$action" == "status" ]]; then
            exec runuser -u "$APP_USER" -- "$PYTHON_BIN" "$helper" status
        fi
        if [[ "$action" == "enable" ]]; then
            nginx_config="$(nginx -T 2>&1)" || {
                echo "Unable to inspect the active Nginx configuration." >&2
                exit 1
            }
            if [[ "$nginx_config" != *"location = /verify {"* ]] \
                || [[ "$nginx_config" != *"location = /verify/complete {"* ]]; then
                echo "Turnstile verification routes are not active in Nginx." >&2
                echo "Configure HTTPS first: sudo tg-bot configure DOMAIN EMAIL [443|8443]" >&2
                exit 1
            fi
        fi

        env_backup="$(mktemp)"
        cp --preserve=mode,ownership,timestamps "$env_file" "$env_backup"
        cleanup_turnstile_backup() {
            rm -f "$env_backup"
        }
        trap cleanup_turnstile_backup EXIT

        if [[ "$action" == "enable" ]]; then
            if ! runuser -u "$APP_USER" -- \
                "$PYTHON_BIN" "$helper" enable </dev/tty; then
                exit 1
            fi
        elif ! runuser -u "$APP_USER" -- \
            "$PYTHON_BIN" "$helper" disable; then
            exit 1
        fi
        chown "$APP_USER:$APP_USER" "$env_file"
        chmod 600 "$env_file"

        if ! restart_and_check; then
            echo "Service health check failed; restoring the previous .env." >&2
            install -o "$APP_USER" -g "$APP_USER" -m 600 \
                "$env_backup" "$env_file"
            systemctl restart "$SERVICE_NAME" || true
            if ! wait_for_health; then
                echo "The service still needs attention after rollback." >&2
            fi
            exit 1
        fi
        rm -f "$env_backup"
        trap - EXIT
        ;;
    configure)
        if [[ $# -lt 2 || $# -gt 3 ]]; then
            usage >&2
            exit 2
        fi
        exec bash "$SCRIPT_DIR/configure-nginx.sh" "$@"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "Unknown command: $command_name" >&2
        usage >&2
        exit 2
        ;;
esac
