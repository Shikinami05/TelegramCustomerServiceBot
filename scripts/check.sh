#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Missing virtual environment: $PROJECT_DIR/venv" >&2
    exit 1
fi

cd "$PROJECT_DIR"
"$PYTHON_BIN" -m py_compile app.py scripts/manage_webhook.py
"$PYTHON_BIN" -m unittest discover -s tests -v

if curl --fail --silent --show-error http://127.0.0.1:9000/healthz; then
    echo
    echo "Health check passed."
else
    echo "Service is not running yet; code checks passed."
fi
