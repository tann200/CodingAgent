#!/usr/bin/env bash
# start.sh - startup script for CodingAgent
# - Sets working directory to the script location (project root)
# - Creates and activates a virtual environment in .venv/
# - Installs requirements from requirements.txt if present (prefers 'uv' if available)
# - Runs the CLI bootstrap via: python -m src.main (fallback: main.py)
# Usage:
#   ./start.sh [args...]
#   bash start.sh -- <args>
# Environment:
#   DRY_RUN=1  -> don't perform network installs or actually exec the app; useful for CI/local checks

set -euo pipefail

# Resolve the directory where the script lives (project root)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR" || exit 1

PROJECT_ROOT="$SCRIPT_DIR"
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQ_FILE="$PROJECT_ROOT/requirements.txt"

DRY_RUN=${DRY_RUN:-0}
ENABLE_TUI=${ENABLE_TUI:-1}
export ENABLE_TUI

echo "[start.sh] Project root: $PROJECT_ROOT"

echo "[start.sh] DRY_RUN=${DRY_RUN}"

# Create virtual environment if missing
if [ ! -d "$VENV_DIR" ]; then
  echo "[start.sh] Creating virtual environment in $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
fi

# Refresh VENV_PYTHON path in case it was just created
VENV_PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  echo "[start.sh] ERROR: expected python executable at $VENV_PYTHON not found" >&2
  exit 1
fi

# Activate virtualenv for interactive shells; still use $VENV_PYTHON for deterministic installs
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate" || true

# Upgrade pip and setuptools using venv python
echo "[start.sh] Upgrading pip/setuptools/wheel in venv..."
"$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel || echo "[start.sh] pip upgrade failed (continuing)" >&2

# If DRY_RUN requested, skip network installs and just report back the actions
if [ "$DRY_RUN" != "0" ]; then
  echo "[start.sh] DRY_RUN enabled: skipping package installation and app exec."
  echo "[start.sh] Would run: $VENV_PYTHON -u -m src.main"
  exit 0
fi

# Install 'uv' dependency manager if available and prefer it to install deps
if [ -f "$REQ_FILE" ]; then
  echo "[start.sh] requirements detected at $REQ_FILE"
  echo "[start.sh] Installing dependency manager 'uv' into venv (best-effort)..."
  "$VENV_PYTHON" -m pip install --upgrade uv || echo "[start.sh] uv install via pip failed or uv not on PyPI; will fallback to pip install" >&2

  # If uv is importable under the venv python, try to run python -m uv install --no-input
  if "$VENV_PYTHON" -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('uv') else 1)" 2>/dev/null; then
    echo "[start.sh] 'uv' found in venv; attempting '$VENV_PYTHON -m uv install --no-input'"
    if "$VENV_PYTHON" -m uv install --no-input; then
      echo "[start.sh] 'uv install' succeeded"
    else
      echo "[start.sh] 'uv install' failed; falling back to pip install -r requirements.txt"
      "$VENV_PYTHON" -m pip install -r "$REQ_FILE"
    fi
  else
    echo "[start.sh] 'uv' not available in venv; using pip to install requirements"
    "$VENV_PYTHON" -m pip install -r "$REQ_FILE"
  fi
else
  echo "[start.sh] No requirements.txt found; skipping dependency installation"
fi

# After installing requirements, perform a small import check for critical packages
CRITICAL_IMPORTS=("textual" "requests" "httpx" "openai" "uv" "jsonschema")
for mod in "${CRITICAL_IMPORTS[@]}"; do
  echo "[start.sh] Checking import for: $mod"
  if ! "$VENV_PYTHON" -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('$mod') else 1)" 2>/dev/null; then
    echo "[start.sh] Module '$mod' not importable in venv; attempting to install via pip..."
    "$VENV_PYTHON" -m pip install --upgrade "$mod" || echo "[start.sh] Failed to install $mod via pip (continuing)" >&2
  else
    echo "[start.sh] Module '$mod' present"
  fi
done

# Determine entrypoint: prefer python -m src.main if available
echo "[start.sh] Locating entrypoint..."
if "$VENV_PYTHON" -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('src.main') else 1)" 2>/dev/null; then
  echo "[start.sh] Running module entrypoint: python -m src.main"
  exec "$VENV_PYTHON" -u -m src.main "$@"
elif [ -f "$PROJECT_ROOT/main.py" ]; then
  echo "[start.sh] Running script entrypoint: main.py"
  exec "$VENV_PYTHON" -u "$PROJECT_ROOT/main.py" "$@"
else
  echo "[start.sh] ERROR: no entrypoint found (module 'src.main' or main.py)." >&2
  exit 1
fi

