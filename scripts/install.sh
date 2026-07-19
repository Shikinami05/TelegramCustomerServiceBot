#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this installer as root: sudo bash scripts/install.sh" >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-lw}"
SERVICE_NAME="${SERVICE_NAME:-tg-bot}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ ! "$PROJECT_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "Unsupported project path: $PROJECT_DIR" >&2
    exit 1
fi
if ! id "$APP_USER" >/dev/null 2>&1; then
    echo "Linux user does not exist: $APP_USER" >&2
    exit 1
fi
if [[ ! -f "$PROJECT_DIR/app.py" ]]; then
    echo "app.py was not found in $PROJECT_DIR" >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer currently supports Debian and Ubuntu." >&2
    exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv curl openssl

chown -R "$APP_USER:$APP_USER" "$PROJECT_DIR"
if [[ ! -d "$PROJECT_DIR/venv" ]]; then
    runuser -u "$APP_USER" -- python3 -m venv "$PROJECT_DIR/venv"
fi
runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/python" -m pip install --upgrade pip
runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    read -r -s -p "BOT_TOKEN: " BOT_TOKEN_INPUT
    echo
    read -r -p "ADMIN_IDS (comma separated): " ADMIN_IDS_INPUT
    read -r -p "Webhook URL (for example https://bot.example.com/tg/webhook): " WEBHOOK_URL_INPUT
    WEBHOOK_SECRET_INPUT="$(openssl rand -hex 32)"

    if [[ -z "$BOT_TOKEN_INPUT" || -z "$ADMIN_IDS_INPUT" || -z "$WEBHOOK_URL_INPUT" ]]; then
        echo "BOT_TOKEN, ADMIN_IDS and Webhook URL are required." >&2
        exit 1
    fi

    install -o "$APP_USER" -g "$APP_USER" -m 600 /dev/null "$PROJECT_DIR/.env"
    {
        printf 'BOT_TOKEN=%s\n' "$BOT_TOKEN_INPUT"
        printf 'WEBHOOK_SECRET=%s\n' "$WEBHOOK_SECRET_INPUT"
        printf 'ADMIN_IDS=%s\n' "$ADMIN_IDS_INPUT"
        printf 'WEBHOOK_URL=%s\n' "$WEBHOOK_URL_INPUT"
        printf '\nDB_BACKUP_ENABLED=true\n'
        printf 'DB_BACKUP_INTERVAL_SECONDS=86400\n'
        printf 'DB_BACKUP_KEEP=14\n'
        printf 'DB_BACKUP_DIR=%s/backups\n' "$PROJECT_DIR"
        printf '\nUSER_RATE_LIMIT_COUNT=8\n'
        printf 'USER_RATE_LIMIT_WINDOW_SECONDS=60\n'
        printf 'USER_RATE_LIMIT_COOLDOWN_SECONDS=300\n'
        printf 'MESSAGE_RETENTION_DAYS=180\n'
        printf '\nBROADCAST_SEND_DELAY_SECONDS=0.05\n'
        printf 'UPDATE_PROCESSING_TIMEOUT_SECONDS=300\n'
        printf 'LOG_LEVEL=INFO\n'
    } > "$PROJECT_DIR/.env"
    chown "$APP_USER:$APP_USER" "$PROJECT_DIR/.env"
    chmod 600 "$PROJECT_DIR/.env"
else
    chown "$APP_USER:$APP_USER" "$PROJECT_DIR/.env"
    chmod 600 "$PROJECT_DIR/.env"
fi

sed \
    -e "s|__APP_USER__|$APP_USER|g" \
    -e "s|__INSTALL_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/deploy/tg-bot.service.example" > "$SERVICE_FILE"
chmod 644 "$SERVICE_FILE"

cd "$PROJECT_DIR"
runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/python" -m py_compile app.py scripts/manage_webhook.py
runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/python" -m unittest discover -s tests -v

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

for _ in {1..20}; do
    if curl --fail --silent http://127.0.0.1:9000/healthz >/dev/null; then
        break
    fi
    sleep 1
done

if ! curl --fail --silent --show-error http://127.0.0.1:9000/healthz; then
    journalctl -u "$SERVICE_NAME" -n 80 --no-pager
    exit 1
fi
echo
echo "Bot service installation complete."
echo "Next: sudo bash scripts/configure-nginx.sh DOMAIN EMAIL"
