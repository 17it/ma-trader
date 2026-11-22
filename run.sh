#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# MA Auto Trader — startup script
# Usage:
#   ./run.sh           — production mode
#   ./run.sh --reload  — development mode (auto-reload on file changes)
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 1. Virtual environment
# ---------------------------------------------------------------------------
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[run.sh] Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# ---------------------------------------------------------------------------
# 2. Install / upgrade dependencies
# ---------------------------------------------------------------------------
if python -c "import fastapi, uvicorn, sqlalchemy, pydantic, ccxt, pandas" &>/dev/null; then
    echo "[run.sh] Dependencies already satisfied. Skipping pip install."
else
    echo "[run.sh] Installing dependencies from requirements.txt (with progress visible) ..."
    # Use Tsinghua mirror with fallback to official PyPI for fast downloads in all regions
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple
fi

# ---------------------------------------------------------------------------
# 3. Load .env if present
# ---------------------------------------------------------------------------
if [ -f "$SCRIPT_DIR/.env" ]; then
    echo "[run.sh] Loading environment from .env ..."
    set -o allexport
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
    set +o allexport
fi

# ---------------------------------------------------------------------------
# 4. Ensure data directory exists
# ---------------------------------------------------------------------------
mkdir -p "$SCRIPT_DIR/data"

# ---------------------------------------------------------------------------
# 5. Launch Uvicorn
# ---------------------------------------------------------------------------
RELOAD_FLAG=""
if [[ "${1:-}" == "--reload" ]]; then
    RELOAD_FLAG="--reload"
    echo "[run.sh] Starting in DEVELOPMENT mode (--reload) ..."
else
    echo "[run.sh] Starting in PRODUCTION mode ..."
fi

exec python -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    $RELOAD_FLAG
