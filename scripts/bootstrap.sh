#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_URL="https://github.com/Shikinami05/TelegramCustomerServiceBot.git"
SERVICE_NAME="${SERVICE_NAME:-tg-bot}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run the one-line installer through sudo." >&2
    exit 1
fi
if [[ ! -r /dev/tty || ! -w /dev/tty ]]; then
    echo "An interactive terminal is required for secure setup prompts." >&2
    exit 1
fi

APP_USER="${APP_USER:-${SUDO_USER:-}}"
if [[ -z "$APP_USER" || "$APP_USER" == "root" ]]; then
    read -r -p "Linux user that will run the Bot: " APP_USER </dev/tty
fi
if [[ -z "$APP_USER" || "$APP_USER" == "root" ]]; then
    echo "The Bot must run as a non-root Linux user." >&2
    exit 1
fi
if ! id "$APP_USER" >/dev/null 2>&1; then
    echo "Linux user does not exist: $APP_USER" >&2
    exit 1
fi

APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
PROJECT_DIR="${PROJECT_DIR:-$APP_HOME/tg-bot}"
if [[ -z "$APP_HOME" || "$APP_HOME" != /* ]]; then
    echo "Unable to determine the home directory for $APP_USER." >&2
    exit 1
fi
if [[ ! "$PROJECT_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "Unsupported project path: $PROJECT_DIR" >&2
    exit 1
fi
USE_EXISTING_CHECKOUT=false
if [[ -e "$PROJECT_DIR" ]]; then
    if [[ ! -d "$PROJECT_DIR/.git" || ! -f "$PROJECT_DIR/app.py" ]]; then
        echo "Installation directory exists but is not a tg-bot checkout:" >&2
        echo "  $PROJECT_DIR" >&2
        exit 1
    fi
    runuser -u "$APP_USER" -- git -C "$PROJECT_DIR" remote set-url \
        origin "$REPOSITORY_URL"
    if [[ -f "$SERVICE_FILE" && -x "$PROJECT_DIR/venv/bin/python" ]]; then
        echo "Existing installation found. Updating it to the latest release..."
        APP_USER="$APP_USER" \
            bash "$PROJECT_DIR/scripts/update.sh" --version latest </dev/tty
        install -o root -g root -m 755 \
            "$PROJECT_DIR/deploy/tg-bot-cli" /usr/local/bin/tg-bot
        echo "Update complete. Future updates: sudo tg-bot update"
        exit 0
    fi
    echo "Existing source checkout found. Continuing installation..."
    USE_EXISTING_CHECKOUT=true
fi

read -r -p "Bot domain (for example bot.example.com): " DOMAIN_NAME </dev/tty
read -r -p "Certificate email: " CERT_EMAIL </dev/tty

DEFAULT_HTTPS_PORT=443
if command -v ss >/dev/null 2>&1 \
    && [[ -n "$(ss -H -ltnp 'sport = :443' 2>/dev/null || true)" ]]; then
    DEFAULT_HTTPS_PORT=8443
fi
read -r -p "HTTPS port [${DEFAULT_HTTPS_PORT}]: " HTTPS_PORT </dev/tty
HTTPS_PORT="${HTTPS_PORT:-$DEFAULT_HTTPS_PORT}"

if [[ ! "$DOMAIN_NAME" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "Invalid domain: $DOMAIN_NAME" >&2
    exit 1
fi
if [[ ! "$CERT_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]]; then
    echo "Invalid certificate email." >&2
    exit 1
fi
if [[ "$HTTPS_PORT" != "443" && "$HTTPS_PORT" != "8443" ]]; then
    echo "HTTPS port must be 443 or 8443." >&2
    exit 1
fi

if [[ "$HTTPS_PORT" == "443" ]]; then
    INSTALL_WEBHOOK_URL="https://${DOMAIN_NAME}/tg/webhook"
else
    INSTALL_WEBHOOK_URL="https://${DOMAIN_NAME}:${HTTPS_PORT}/tg/webhook"
fi

if ! command -v git >/dev/null 2>&1; then
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "This installer currently supports Debian and Ubuntu." >&2
        exit 1
    fi
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates git
fi

if [[ "$USE_EXISTING_CHECKOUT" == "false" ]]; then
    runuser -u "$APP_USER" -- git clone "$REPOSITORY_URL" "$PROJECT_DIR"
fi

APP_USER="$APP_USER" INSTALL_WEBHOOK_URL="$INSTALL_WEBHOOK_URL" \
    bash "$PROJECT_DIR/scripts/install.sh" --version latest </dev/tty
APP_USER="$APP_USER" \
    bash "$PROJECT_DIR/scripts/configure-nginx.sh" \
    "$DOMAIN_NAME" "$CERT_EMAIL" "$HTTPS_PORT" </dev/tty

echo
echo "Installation complete."
echo "Future updates: sudo tg-bot update"
echo "Available commands: sudo tg-bot help"
