#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash scripts/configure-nginx.sh DOMAIN EMAIL" >&2
    exit 1
fi
if [[ "$#" -ne 2 ]]; then
    echo "Usage: sudo bash scripts/configure-nginx.sh bot.example.com admin@example.com" >&2
    exit 1
fi

DOMAIN_NAME="$1"
CERT_EMAIL="$2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-tg-bot}"
NGINX_SITE="/etc/nginx/sites-available/tg-bot"

source "$SCRIPT_DIR/common.sh"
resolve_app_identity "$SERVICE_NAME"

if [[ ! "$DOMAIN_NAME" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "Invalid domain: $DOMAIN_NAME" >&2
    exit 1
fi
if [[ ! "$CERT_EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]]; then
    echo "Invalid email: $CERT_EMAIL" >&2
    exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot python3-certbot-nginx

sed "s|__DOMAIN__|$DOMAIN_NAME|g" "$PROJECT_DIR/deploy/nginx.conf.example" > "$NGINX_SITE"
ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/tg-bot
nginx -t
systemctl enable --now nginx
systemctl reload nginx

certbot --nginx --non-interactive --agree-tos --redirect \
    --email "$CERT_EMAIL" -d "$DOMAIN_NAME"
nginx -t
systemctl reload nginx
runuser -u "$APP_USER" -- \
    "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/scripts/manage_webhook.py"
echo "Nginx and HTTPS are configured for $DOMAIN_NAME."
