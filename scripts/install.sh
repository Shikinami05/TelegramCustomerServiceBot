#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this installer as root: sudo bash scripts/install.sh" >&2
    exit 1
fi

usage() {
    cat <<'EOF'
Usage: sudo bash scripts/install.sh [--version latest|v1.2.3]

Without --version, installs the currently checked-out code.
EOF
}

REQUESTED_VERSION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            if [[ $# -lt 2 ]]; then
                echo "--version requires a value." >&2
                exit 2
            fi
            REQUESTED_VERSION="$2"
            shift 2
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
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-tg-bot}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

source "$SCRIPT_DIR/common.sh"
resolve_app_identity "$SERVICE_NAME"

if [[ ! "$PROJECT_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "Unsupported project path: $PROJECT_DIR" >&2
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
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv curl git openssl

chown -R "$APP_USER:$APP_USER" "$PROJECT_DIR"
if [[ -n "$REQUESTED_VERSION" ]]; then
    require_clean_git_checkout "$PROJECT_DIR"
    resolve_release_tag "$PROJECT_DIR" "$REQUESTED_VERSION"
    git_as_app -C "$PROJECT_DIR" checkout --detach "$RELEASE_COMMIT"
    echo "Selected release $RELEASE_TAG ($RELEASE_COMMIT)"
fi
if [[ ! -f "$PROJECT_DIR/VERSION" || ! -f "$PROJECT_DIR/app.py" ]]; then
    echo "The selected checkout is not an installable release." >&2
    exit 1
fi

if [[ ! -d "$PROJECT_DIR/venv" ]]; then
    runuser -u "$APP_USER" -- python3 -m venv "$PROJECT_DIR/venv"
fi
runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/python" -m pip install --upgrade pip
runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    read -r -s -p "BOT_TOKEN: " BOT_TOKEN_INPUT
    echo
    read -r -p "ADMIN_IDS (comma separated): " ADMIN_IDS_INPUT
    WEBHOOK_URL_INPUT="${INSTALL_WEBHOOK_URL:-}"
    if [[ -z "$WEBHOOK_URL_INPUT" ]]; then
        read -r -p "Webhook URL (for example https://bot.example.com/tg/webhook): " WEBHOOK_URL_INPUT
    fi
    WEBHOOK_SECRET_INPUT="$(openssl rand -hex 32)"
    TURNSTILE_ENABLED_INPUT=false
    TURNSTILE_SITE_KEY_INPUT=""
    TURNSTILE_SECRET_KEY_INPUT=""
    read -r -p "Enable Cloudflare Turnstile before users can leave messages? [y/N]: " \
        TURNSTILE_CHOICE
    case "${TURNSTILE_CHOICE,,}" in
        y|yes)
            TURNSTILE_ENABLED_INPUT=true
            read -r -p "Cloudflare Turnstile Site Key: " TURNSTILE_SITE_KEY_INPUT
            read -r -s -p "Cloudflare Turnstile Secret Key: " \
                TURNSTILE_SECRET_KEY_INPUT
            echo
            ;;
        ""|n|no)
            ;;
        *)
            echo "Turnstile choice must be y or n." >&2
            exit 2
            ;;
    esac

    if [[ -z "$BOT_TOKEN_INPUT" || -z "$ADMIN_IDS_INPUT" || -z "$WEBHOOK_URL_INPUT" ]]; then
        echo "BOT_TOKEN, ADMIN_IDS and Webhook URL are required." >&2
        exit 1
    fi
    if [[ ! "$WEBHOOK_URL_INPUT" =~ ^https://[A-Za-z0-9.-]+(:443|:8443)?/tg/webhook$ ]]; then
        echo "Webhook URL must use an HTTPS domain, optional port 443 or 8443, and end with /tg/webhook." >&2
        exit 1
    fi
    TURNSTILE_VERIFY_URL_INPUT="${WEBHOOK_URL_INPUT%/tg/webhook}/verify"
    if [[ "$TURNSTILE_ENABLED_INPUT" == "true" ]]; then
        if [[ -z "$TURNSTILE_SITE_KEY_INPUT" || -z "$TURNSTILE_SECRET_KEY_INPUT" ]]; then
            echo "Turnstile Site Key and Secret Key are required." >&2
            exit 1
        fi
        if [[ "$TURNSTILE_SITE_KEY_INPUT" =~ [[:space:]] \
            || "$TURNSTILE_SECRET_KEY_INPUT" =~ [[:space:]] ]]; then
            echo "Turnstile keys must not contain whitespace." >&2
            exit 1
        fi
    fi

    install -o "$APP_USER" -g "$APP_USER" -m 600 /dev/null "$PROJECT_DIR/.env"
    {
        printf 'BOT_TOKEN=%s\n' "$BOT_TOKEN_INPUT"
        printf 'WEBHOOK_SECRET=%s\n' "$WEBHOOK_SECRET_INPUT"
        printf 'ADMIN_IDS=%s\n' "$ADMIN_IDS_INPUT"
        printf 'OWNER_IDS=%s\n' "$ADMIN_IDS_INPUT"
        printf 'WEBHOOK_URL=%s\n' "$WEBHOOK_URL_INPUT"
        printf '\nTURNSTILE_ENABLED=%s\n' "$TURNSTILE_ENABLED_INPUT"
        printf 'TURNSTILE_SITE_KEY=%s\n' "$TURNSTILE_SITE_KEY_INPUT"
        printf 'TURNSTILE_SECRET_KEY=%s\n' "$TURNSTILE_SECRET_KEY_INPUT"
        printf 'TURNSTILE_VERIFY_URL=%s\n' "$TURNSTILE_VERIFY_URL_INPUT"
        printf 'TURNSTILE_VERIFY_DAYS=30\n'
        printf 'TURNSTILE_INIT_DATA_MAX_AGE_SECONDS=600\n'
        printf '\nDB_BACKUP_ENABLED=true\n'
        printf 'DB_BACKUP_INTERVAL_SECONDS=86400\n'
        printf 'DB_BACKUP_KEEP=14\n'
        printf 'DB_BACKUP_DIR=%s/backups\n' "$PROJECT_DIR"
        printf '\nUSER_RATE_LIMIT_COUNT=8\n'
        printf 'USER_RATE_LIMIT_WINDOW_SECONDS=60\n'
        printf 'USER_RATE_LIMIT_COOLDOWN_SECONDS=300\n'
        printf 'MESSAGE_RETENTION_DAYS=180\n'
        printf '\nBROADCAST_SEND_DELAY_SECONDS=0.05\n'
        printf 'BROADCAST_RATE_LIMIT_RETRIES=3\n'
        printf 'UPDATE_PROCESSING_TIMEOUT_SECONDS=300\n'
        printf 'PENDING_REMINDER_MINUTES=30\n'
        printf 'TELEGRAM_INLINE_RETRY_MAX_SECONDS=5\n'
        printf 'DISPLAY_TIMEZONE=Asia/Hong_Kong\n'
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
runuser -u "$APP_USER" -- "$PROJECT_DIR/venv/bin/python" -m py_compile \
    app.py tg_bot/*.py scripts/manage_webhook.py scripts/manage_backup.py \
    scripts/manage_turnstile.py
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
install -o root -g root -m 755 \
    "$PROJECT_DIR/deploy/tg-bot-cli" /usr/local/bin/tg-bot
echo
echo "Bot service installation complete."
echo "Next: sudo tg-bot configure DOMAIN EMAIL [443|8443]"
