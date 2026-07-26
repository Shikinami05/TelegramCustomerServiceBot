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

git_as_app() {
    runuser -u "$APP_USER" -- git "$@"
}

validate_release_tag() {
    local version="$1"
    [[ "$version" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
}

require_clean_git_checkout() {
    local project_dir="$1"

    if [[ ! -d "$project_dir/.git" ]]; then
        echo "The project must be a Git checkout: $project_dir" >&2
        return 1
    fi
    if [[ -n "$(git_as_app -C "$project_dir" status --porcelain)" ]]; then
        echo "The Git checkout has local changes; operation aborted." >&2
        return 1
    fi
}

resolve_release_tag() {
    local project_dir="$1"
    local requested_version="$2"
    local available_tags=""
    local candidate=""
    local declared_version=""
    local remote_ref=""
    local remote_refs=""
    local remote_sha=""

    if [[ "$requested_version" != "latest" ]] \
        && ! validate_release_tag "$requested_version"; then
        echo "Version must be 'latest' or a stable tag such as v1.2.3." >&2
        return 1
    fi

    remote_refs="$(
        git_as_app -C "$project_dir" ls-remote --tags --refs origin 'v*'
    )"
    while read -r remote_sha remote_ref; do
        candidate="${remote_ref#refs/tags/}"
        if validate_release_tag "$candidate"; then
            available_tags+="${candidate}"$'\n'
        fi
    done <<< "$remote_refs"

    if [[ "$requested_version" == "latest" ]]; then
        requested_version="$(
            printf '%s' "$available_tags" |
                sort --version-sort --reverse |
                sed -n '1p'
        )"
        if [[ -z "$requested_version" ]]; then
            echo "No stable release tags were found." >&2
            return 1
        fi
    elif ! grep -Fxq "$requested_version" <<< "$available_tags"; then
        echo "Release tag does not exist on origin: $requested_version" >&2
        return 1
    fi

    git_as_app -C "$project_dir" fetch origin \
        "refs/tags/${requested_version}:refs/tags/${requested_version}"
    if [[ "$(
        git_as_app -C "$project_dir" cat-file -t \
            "refs/tags/${requested_version}"
    )" != "tag" ]]; then
        echo "Release tag must be an annotated tag: $requested_version" >&2
        return 1
    fi
    RELEASE_COMMIT="$(
        git_as_app -C "$project_dir" rev-parse \
            --verify "refs/tags/${requested_version}^{commit}"
    )"
    declared_version="$(
        git_as_app -C "$project_dir" show "${RELEASE_COMMIT}:VERSION" |
            tr -d '[:space:]'
    )"
    if [[ "v${declared_version}" != "$requested_version" ]]; then
        echo "Tag $requested_version does not match VERSION ($declared_version)." >&2
        return 1
    fi

    RELEASE_TAG="$requested_version"
    export RELEASE_TAG RELEASE_COMMIT
}
