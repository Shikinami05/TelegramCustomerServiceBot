#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash scripts/configure-nginx.sh DOMAIN EMAIL [HTTPS_PORT]" >&2
    exit 1
fi
if [[ "$#" -lt 2 || "$#" -gt 3 ]]; then
    echo "Usage: sudo bash scripts/configure-nginx.sh bot.example.com admin@example.com [443|8443]" >&2
    exit 1
fi

DOMAIN_NAME="$1"
CERT_EMAIL="$2"
HTTPS_PORT="${3:-${HTTPS_PORT:-443}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-tg-bot}"
NGINX_SITE="/etc/nginx/sites-available/tg-bot"
NGINX_SITE_BACKUP="${NGINX_SITE}.previous"
ACME_WEBROOT="/var/www/tg-bot-acme"
CERT_DIR="/etc/letsencrypt/live/${DOMAIN_NAME}"
ENV_FILE="$PROJECT_DIR/.env"
RENDERED_CONFIG="$(mktemp)"

trap 'rm -f "$RENDERED_CONFIG"' EXIT

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
if [[ "$HTTPS_PORT" != "443" && "$HTTPS_PORT" != "8443" ]]; then
    echo "HTTPS_PORT must be 443 or 8443 for Telegram webhooks." >&2
    exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
    echo ".env was not found in $PROJECT_DIR; run scripts/install.sh first." >&2
    exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot python3-certbot-nginx
install -d -o www-data -g www-data -m 755 "$ACME_WEBROOT"

if command -v ss >/dev/null 2>&1; then
    PORT_OWNER="$(ss -H -ltnp "sport = :${HTTPS_PORT}" 2>/dev/null || true)"
    NON_NGINX_OWNER="$(printf '%s\n' "$PORT_OWNER" | grep -v nginx || true)"
    if [[ -n "$NON_NGINX_OWNER" ]]; then
        echo "Port $HTTPS_PORT is already used by another service:" >&2
        echo "$NON_NGINX_OWNER" >&2
        if [[ "$HTTPS_PORT" == "443" ]]; then
            echo "Use port 8443 instead: sudo bash scripts/configure-nginx.sh $DOMAIN_NAME $CERT_EMAIL 8443" >&2
        fi
        exit 1
    fi
fi

render_config() {
    local template="$1"
    local redirect_port=""
    if [[ "$HTTPS_PORT" != "443" ]]; then
        redirect_port=":$HTTPS_PORT"
    fi
    sed \
        -e "s|__DOMAIN__|$DOMAIN_NAME|g" \
        -e "s|__ACME_WEBROOT__|$ACME_WEBROOT|g" \
        -e "s|__HTTPS_PORT__|$HTTPS_PORT|g" \
        -e "s|__HTTPS_REDIRECT_PORT__|$redirect_port|g" \
        "$template" > "$RENDERED_CONFIG"
}

activate_config() {
    if [[ -f "$NGINX_SITE" ]]; then
        cp -a "$NGINX_SITE" "$NGINX_SITE_BACKUP"
    fi
    install -m 644 "$RENDERED_CONFIG" "$NGINX_SITE"
    ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/tg-bot
    if ! nginx -t; then
        if [[ -f "$NGINX_SITE_BACKUP" ]]; then
            cp -a "$NGINX_SITE_BACKUP" "$NGINX_SITE"
        else
            rm -f "$NGINX_SITE"
            rm -f /etc/nginx/sites-enabled/tg-bot
        fi
        echo "Nginx configuration failed; the previous site was restored." >&2
        exit 1
    fi
    systemctl enable nginx
    if systemctl is-active --quiet nginx; then
        systemctl reload nginx
    else
        systemctl start nginx
    fi
}

CERT_ALREADY_EXISTS=false
if [[ -f "$CERT_DIR/fullchain.pem" && -f "$CERT_DIR/privkey.pem" ]]; then
    CERT_ALREADY_EXISTS=true
    render_config "$PROJECT_DIR/deploy/nginx.conf.example"
else
    render_config "$PROJECT_DIR/deploy/nginx-http.conf.example"
fi
activate_config

CERT_RENEWAL_RECONFIGURED=false
if [[ "$CERT_ALREADY_EXISTS" == "true" ]] \
    && certbot help reconfigure >/dev/null 2>&1; then
    if certbot reconfigure --cert-name "$DOMAIN_NAME" \
        --webroot --webroot-path "$ACME_WEBROOT" --non-interactive; then
        CERT_RENEWAL_RECONFIGURED=true
    else
        echo "Warning: Certbot could not switch renewal to Webroot." >&2
    fi
fi

if [[ "$CERT_ALREADY_EXISTS" != "true" ]]; then
    certbot certonly --webroot --webroot-path "$ACME_WEBROOT" \
        --non-interactive --agree-tos --keep-until-expiring \
        --cert-name "$DOMAIN_NAME" --email "$CERT_EMAIL" -d "$DOMAIN_NAME"
elif [[ "$CERT_RENEWAL_RECONFIGURED" != "true" ]]; then
    echo "The Nginx authenticator plugin remains installed for renewal compatibility." >&2
fi

render_config "$PROJECT_DIR/deploy/nginx.conf.example"
activate_config

if command -v ss >/dev/null 2>&1; then
    HTTPS_LISTENER="$(ss -H -ltnp "sport = :${HTTPS_PORT}" 2>/dev/null || true)"
    if [[ -z "$HTTPS_LISTENER" || "$HTTPS_LISTENER" != *nginx* ]]; then
        echo "Nginx did not acquire HTTPS port $HTTPS_PORT after reload." >&2
        echo "${HTTPS_LISTENER:-No listener found}" >&2
        exit 1
    fi
fi

if [[ "$HTTPS_PORT" == "443" ]]; then
    WEBHOOK_URL_VALUE="https://${DOMAIN_NAME}/tg/webhook"
    TURNSTILE_VERIFY_URL_VALUE="https://${DOMAIN_NAME}/verify"
else
    WEBHOOK_URL_VALUE="https://${DOMAIN_NAME}:${HTTPS_PORT}/tg/webhook"
    TURNSTILE_VERIFY_URL_VALUE="https://${DOMAIN_NAME}:${HTTPS_PORT}/verify"
fi

if grep -q '^WEBHOOK_URL=' "$ENV_FILE"; then
    sed -i "s|^WEBHOOK_URL=.*$|WEBHOOK_URL=$WEBHOOK_URL_VALUE|" "$ENV_FILE"
else
    printf '\nWEBHOOK_URL=%s\n' "$WEBHOOK_URL_VALUE" >> "$ENV_FILE"
fi
if grep -q '^TURNSTILE_VERIFY_URL=' "$ENV_FILE"; then
    sed -i \
        "s|^TURNSTILE_VERIFY_URL=.*$|TURNSTILE_VERIFY_URL=$TURNSTILE_VERIFY_URL_VALUE|" \
        "$ENV_FILE"
else
    printf 'TURNSTILE_VERIFY_URL=%s\n' "$TURNSTILE_VERIFY_URL_VALUE" >> "$ENV_FILE"
fi
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

systemctl restart "$SERVICE_NAME"
for _ in {1..20}; do
    if curl --fail --silent http://127.0.0.1:9000/healthz >/dev/null; then
        break
    fi
    sleep 1
done
if ! curl --fail --silent --show-error http://127.0.0.1:9000/healthz >/dev/null; then
    journalctl -u "$SERVICE_NAME" -n 80 --no-pager
    exit 1
fi

runuser -u "$APP_USER" -- \
    "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/scripts/manage_webhook.py"

echo "Nginx, HTTPS, webhook, and command menus are configured."
echo "Public webhook: $WEBHOOK_URL_VALUE"
if [[ "$HTTPS_PORT" == "8443" ]]; then
    echo "Ensure TCP port 8443 is allowed by UFW and the VPS provider firewall."
fi
