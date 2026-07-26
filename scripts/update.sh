#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo bash scripts/update.sh" >&2
    exit 1
fi

usage() {
    cat <<'EOF'
Usage: sudo bash scripts/update.sh [--version latest|v1.2.3]

Without --version, fast-forwards the current branch to its upstream.
With --version, deploys an exact stable release tag.
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
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
ROLLBACK_BACKUP_KEEP="${ROLLBACK_BACKUP_KEEP:-5}"
RENDERED_SERVICE="$(mktemp)"
SERVICE_BACKUP="$(mktemp)"
BACKUP_HELPER="$(mktemp)"
ROLLBACK_ARMED=false
SERVICE_EXISTED=false
DATABASE_EXISTED=false
ROLLBACK_DB=""
PREVIOUS_COMMIT=""
PREVIOUS_BRANCH=""

source "$SCRIPT_DIR/common.sh"

wait_for_health() {
    local healthy=false
    for _ in {1..20}; do
        if curl --fail --silent http://127.0.0.1:9000/healthz >/dev/null; then
            healthy=true
            break
        fi
        sleep 1
    done
    [[ "$healthy" == "true" ]]
}

rollback_update() {
    local rollback_failed=false
    set +e
    echo "Update failed. Restoring the previous deployment..." >&2

    systemctl stop "$SERVICE_NAME" || rollback_failed=true

    if [[ -n "$PREVIOUS_BRANCH" ]]; then
        git_as_app -C "$PROJECT_DIR" checkout "$PREVIOUS_BRANCH" \
            || rollback_failed=true
        git_as_app -C "$PROJECT_DIR" reset --hard "$PREVIOUS_COMMIT" \
            || rollback_failed=true
    else
        git_as_app -C "$PROJECT_DIR" checkout --detach "$PREVIOUS_COMMIT" \
            || rollback_failed=true
    fi

    if [[ -f "$PROJECT_DIR/requirements.txt" ]]; then
        runuser -u "$APP_USER" -- \
            "$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" \
            || rollback_failed=true
    else
        rollback_failed=true
    fi

    if [[ -n "$ROLLBACK_DB" && -f "$ROLLBACK_DB" ]]; then
        runuser -u "$APP_USER" -- "$PYTHON_BIN" "$BACKUP_HELPER" restore \
            --database "$PROJECT_DIR/bot.db" \
            --input "$ROLLBACK_DB" \
            || rollback_failed=true
    elif [[ "$DATABASE_EXISTED" == "false" ]]; then
        rm -f \
            "$PROJECT_DIR/bot.db" \
            "$PROJECT_DIR/bot.db-wal" \
            "$PROJECT_DIR/bot.db-shm" \
            || rollback_failed=true
    fi

    if [[ "$SERVICE_EXISTED" == "true" ]]; then
        install -m 644 "$SERVICE_BACKUP" "$SERVICE_FILE" \
            || rollback_failed=true
    else
        rm -f "$SERVICE_FILE" || rollback_failed=true
    fi
    systemctl daemon-reload || rollback_failed=true
    systemctl restart "$SERVICE_NAME" || rollback_failed=true
    wait_for_health || rollback_failed=true

    if [[ "$rollback_failed" == "true" ]]; then
        echo "Automatic rollback was incomplete. Inspect the service immediately:" >&2
        echo "  journalctl -u $SERVICE_NAME -n 100 --no-pager" >&2
        return 1
    fi
    echo "Rollback complete. The previous deployment is healthy again." >&2
}

finish() {
    local status=$?
    local final_status="$status"
    trap - EXIT
    if [[ "$status" -ne 0 && "$ROLLBACK_ARMED" == "true" ]]; then
        if ! rollback_update; then
            final_status=70
        fi
    fi
    rm -f "$RENDERED_SERVICE" "$SERVICE_BACKUP" "$BACKUP_HELPER"
    exit "$final_status"
}
trap finish EXIT

resolve_app_identity "$SERVICE_NAME"

if [[ ! "$PROJECT_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "Unsupported project path: $PROJECT_DIR" >&2
    exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "The project must have an installed virtual environment." >&2
    exit 1
fi
if [[ ! -f "$SERVICE_FILE" ]]; then
    echo "The systemd service file does not exist: $SERVICE_FILE" >&2
    exit 1
fi
if [[ ! "$ROLLBACK_BACKUP_KEEP" =~ ^[1-9][0-9]*$ ]]; then
    echo "ROLLBACK_BACKUP_KEEP must be a positive integer." >&2
    exit 1
fi
require_clean_git_checkout "$PROJECT_DIR"

PREVIOUS_COMMIT="$(git_as_app -C "$PROJECT_DIR" rev-parse HEAD)"
PREVIOUS_BRANCH="$(
    git_as_app -C "$PROJECT_DIR" symbolic-ref --quiet --short HEAD || true
)"

TARGET_COMMIT=""
TARGET_LABEL=""
if [[ -n "$REQUESTED_VERSION" ]]; then
    resolve_release_tag "$PROJECT_DIR" "$REQUESTED_VERSION"
    TARGET_COMMIT="$RELEASE_COMMIT"
    TARGET_LABEL="$RELEASE_TAG"
else
    if [[ -z "$PREVIOUS_BRANCH" ]]; then
        echo "Detached deployments require --version latest or --version v1.2.3." >&2
        exit 1
    fi
    git_as_app -C "$PROJECT_DIR" fetch --prune
    TARGET_COMMIT="$(
        git_as_app -C "$PROJECT_DIR" rev-parse --verify '@{upstream}^{commit}'
    )"
    if ! git_as_app -C "$PROJECT_DIR" merge-base \
        --is-ancestor "$PREVIOUS_COMMIT" "$TARGET_COMMIT"; then
        echo "The upstream branch is not a fast-forward; update aborted." >&2
        exit 1
    fi
    TARGET_LABEL="$PREVIOUS_BRANCH"
fi

install -o "$APP_USER" -g "$APP_USER" -m 600 \
    "$PROJECT_DIR/scripts/manage_backup.py" "$BACKUP_HELPER"
if [[ -f "$SERVICE_FILE" ]]; then
    cp "$SERVICE_FILE" "$SERVICE_BACKUP"
    SERVICE_EXISTED=true
fi

if [[ -f "$PROJECT_DIR/bot.db" ]]; then
    DATABASE_EXISTED=true
    rollback_dir="$PROJECT_DIR/backups/rollback"
    install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$rollback_dir"
    ROLLBACK_DB="$rollback_dir/rollback-$(
        date -u +%Y%m%dT%H%M%SZ
    )-${PREVIOUS_COMMIT:0:12}-$$.db"
    runuser -u "$APP_USER" -- "$PYTHON_BIN" "$BACKUP_HELPER" create \
        --database "$PROJECT_DIR/bot.db" \
        --output "$ROLLBACK_DB" \
        --keep "$ROLLBACK_BACKUP_KEEP"
fi

ROLLBACK_ARMED=true
if [[ -n "$REQUESTED_VERSION" ]]; then
    git_as_app -C "$PROJECT_DIR" checkout --detach "$TARGET_COMMIT"
else
    git_as_app -C "$PROJECT_DIR" merge --ff-only "$TARGET_COMMIT"
fi

if [[ ! -f "$PROJECT_DIR/VERSION" || ! -f "$PROJECT_DIR/app.py" ]]; then
    echo "The selected target is not an installable release." >&2
    exit 1
fi

cd "$PROJECT_DIR"
runuser -u "$APP_USER" -- \
    "$PROJECT_DIR/venv/bin/pip" install -r requirements.txt
runuser -u "$APP_USER" -- "$PYTHON_BIN" -m py_compile \
    app.py scripts/manage_webhook.py scripts/manage_backup.py
runuser -u "$APP_USER" -- "$PYTHON_BIN" -m unittest discover -s tests -v
runuser -u "$APP_USER" -- bash scripts/version.sh --short

sed \
    -e "s|__APP_USER__|$APP_USER|g" \
    -e "s|__INSTALL_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/deploy/tg-bot.service.example" > "$RENDERED_SERVICE"
install -m 644 "$RENDERED_SERVICE" "$SERVICE_FILE"
systemctl daemon-reload
systemctl restart "$SERVICE_NAME"

if ! wait_for_health; then
    journalctl -u "$SERVICE_NAME" -n 80 --no-pager
    false
fi

install -o root -g root -m 755 \
    "$PROJECT_DIR/deploy/tg-bot-cli" /usr/local/bin/tg-bot

ROLLBACK_ARMED=false
if ! runuser -u "$APP_USER" -- \
    "$PYTHON_BIN" "$PROJECT_DIR/scripts/manage_webhook.py" --commands-only; then
    echo "Warning: code was updated, but Telegram command menu sync failed." >&2
fi

echo "Update complete: $TARGET_LABEL ($TARGET_COMMIT)"
