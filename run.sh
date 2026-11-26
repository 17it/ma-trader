#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# MA Auto Trader — startup script
# Usage:
#   ./run.sh           — production mode (listens on 127.0.0.1:8888 by default, override via PORT=8080)
#   ./run.sh --reload  — development mode (auto-reload on file changes)
#   HOST=0.0.0.0 ./run.sh — override bind address explicitly
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[run.sh] Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

if python -c "import fastapi, uvicorn, sqlalchemy, pydantic, ccxt, pandas, httpx" &>/dev/null; then
    echo "[run.sh] Dependencies already satisfied. Skipping pip install."
else
    echo "[run.sh] Installing dependencies from requirements.txt ..."
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple
fi

if [ -f "$SCRIPT_DIR/.env" ]; then
    echo "[run.sh] Loading environment from .env ..."
    set -o allexport
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
    set +o allexport
fi

mkdir -p "$SCRIPT_DIR/data"
chmod 700 "$SCRIPT_DIR/data"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8888}"

RELOAD_FLAG=""
if [[ "${1:-}" == "--reload" ]]; then
    RELOAD_FLAG="--reload"
    echo "[run.sh] Starting in DEVELOPMENT mode (--reload) on ${HOST}:${PORT} ..."
else
    echo "[run.sh] Starting in PRODUCTION mode on ${HOST}:${PORT} ..."
fi

exec python -m uvicorn backend.main:app \
    --host "$HOST" \
    --port "$PORT" \
    $RELOAD_FLAG
