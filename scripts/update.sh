#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash scripts/update.sh" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-tg-bot}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
RENDERED_SERVICE="$(mktemp)"

trap 'rm -f "$RENDERED_SERVICE"' EXIT

source "$SCRIPT_DIR/common.sh"
resolve_app_identity "$SERVICE_NAME"

if [[ ! "$PROJECT_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "Unsupported project path: $PROJECT_DIR" >&2
    exit 1
fi
if [[ ! -x "$PYTHON_BIN" || ! -d "$PROJECT_DIR/.git" ]]; then
    echo "The project must be a Git checkout with an installed virtual environment." >&2
    exit 1
fi
if [[ -n "$(runuser -u "$APP_USER" -- git -C "$PROJECT_DIR" status --porcelain)" ]]; then
    echo "The Git checkout has local changes; update aborted." >&2
    exit 1
fi

cd "$PROJECT_DIR"
runuser -u "$APP_USER" -- "$PYTHON_BIN" -c \
    "import app; app.init_db(); print(app.backup_database() or 'backup disabled')"
runuser -u "$APP_USER" -- git -C "$PROJECT_DIR" pull --ff-only
runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/pip" install -r requirements.txt
runuser -u "$APP_USER" -- "$PYTHON_BIN" -m py_compile app.py scripts/manage_webhook.py
runuser -u "$APP_USER" -- "$PYTHON_BIN" -m unittest discover -s tests -v

sed \
    -e "s|__APP_USER__|$APP_USER|g" \
    -e "s|__INSTALL_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/deploy/tg-bot.service.example" > "$RENDERED_SERVICE"
install -m 644 "$RENDERED_SERVICE" "$SERVICE_FILE"
systemctl daemon-reload
systemctl restart "$SERVICE_NAME"
service_healthy=false
for _ in {1..20}; do
    if curl --fail --silent http://127.0.0.1:9000/healthz >/dev/null; then
        service_healthy=true
        break
    fi
    sleep 1
done

if [[ "$service_healthy" != "true" ]]; then
    journalctl -u "$SERVICE_NAME" -n 80 --no-pager
    exit 1
fi

if ! runuser -u "$APP_USER" -- \
    "$PYTHON_BIN" "$PROJECT_DIR/scripts/manage_webhook.py" --commands-only; then
    echo "Warning: code was updated, but Telegram command menu sync failed." >&2
fi

echo "Update complete."
