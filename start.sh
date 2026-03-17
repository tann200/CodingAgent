#!/usr/bin/env bash
# start.sh - startup script for CodingAgent
# Updated behavior:
# - Prefer python3.11 when creating venv
# - Only install requirements if requirements.txt changed (cache sha256 in .venv/.requirements.sha256)
# - Do not attempt to pip-install each critical module every run; instead list missing modules and
#   install them only if AUTO_INSTALL=1 or FORCE_INSTALL=1 is set.
# - Preserve DRY_RUN to test behavior without network actions

set -euo pipefail

# Resolve the directory where the script lives (project root)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR" || exit 1

PROJECT_ROOT="$SCRIPT_DIR"
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQ_FILE="$PROJECT_ROOT/requirements.txt"
REQ_HASH_FILE="$VENV_DIR/.requirements.sha256"

DRY_RUN=${DRY_RUN:-0}
AUTO_INSTALL=${AUTO_INSTALL:-0}   # if 1, automatically install missing critical modules
FORCE_INSTALL=${FORCE_INSTALL:-0} # if 1, force reinstall requirements regardless of hash
ENABLE_TUI=${ENABLE_TUI:-1}
export ENABLE_TUI

# Prefer python3.11 if available
PYTHON_CMD=""
if command -v python3.11 >/dev/null 2>&1; then
  PYTHON_CMD=python3.11
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=python3
else
  PYTHON_CMD=python
fi

echo "[start.sh] Project root: $PROJECT_ROOT"
echo "[start.sh] Using python command: $PYTHON_CMD"
echo "[start.sh] DRY_RUN=${DRY_RUN} AUTO_INSTALL=${AUTO_INSTALL} FORCE_INSTALL=${FORCE_INSTALL}"

# Create virtual environment if missing
if [ ! -d "$VENV_DIR" ]; then
  echo "[start.sh] Creating virtual environment in $VENV_DIR using $PYTHON_CMD..."
  $PYTHON_CMD -m venv "$VENV_DIR"
fi

# Refresh VENV_PYTHON path in case it was just created
VENV_PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  echo "[start.sh] ERROR: expected python executable at $VENV_PYTHON not found" >&2
  exit 1
fi

# Activate virtualenv for interactive shells; still use $VENV_PYTHON for deterministic exec
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate" || true

# Optionally upgrade pip if explicitly requested via environment to avoid doing it on every start
if [ "${VENV_PIP_UPGRADE:-0}" = "1" ]; then
  echo "[start.sh] Upgrading pip/setuptools/wheel in venv..."
  "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel || echo "[start.sh] pip upgrade failed (continuing)" >&2
fi

# If DRY_RUN requested, skip network installs and just report back the actions
if [ "$DRY_RUN" != "0" ]; then
  echo "[start.sh] DRY_RUN enabled: skipping package installation and app exec."
  echo "[start.sh] Would run: $VENV_PYTHON -u -m src.main"
  exit 0
fi

# Helper: compute sha256 of requirements.txt (empty string if missing)
_compute_req_hash() {
  if [ -f "$REQ_FILE" ]; then
    if command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "$REQ_FILE" | awk '{print $1}'
    else
      openssl dgst -sha256 "$REQ_FILE" | awk '{print $2}'
    fi
  else
    echo ""
  fi
}

REQ_HASH="$(_compute_req_hash)"
OLD_HASH=""
if [ -f "$REQ_HASH_FILE" ]; then
  OLD_HASH=$(cat "$REQ_HASH_FILE" || echo "")
fi

if [ -f "$REQ_FILE" ]; then
  echo "[start.sh] requirements detected at $REQ_FILE"
  if [ "$FORCE_INSTALL" = "1" ] || [ "$REQ_HASH" != "$OLD_HASH" ]; then
    echo "[start.sh] Requirements changed or FORCE_INSTALL set; installing dependencies..."

    # Try uv if present in venv (best-effort) otherwise pip
    echo "[start.sh] Attempting to use 'uv' if available (best-effort)"
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

    # Save the new hash on success
    echo "$REQ_HASH" > "$REQ_HASH_FILE" || true
  else
    echo "[start.sh] Requirements unchanged; skipping install. (set FORCE_INSTALL=1 to force)"
  fi
else
  echo "[start.sh] No requirements.txt found; skipping dependency installation"
fi

# After installing requirements (or skipping), perform a small import check for critical packages
CRITICAL_IMPORTS=("textual" "requests" "httpx" "openai" "uv" "jsonschema")
MISSING=()
for mod in "${CRITICAL_IMPORTS[@]}"; do
  echo "[start.sh] Checking import for: $mod"
  if ! "$VENV_PYTHON" -c "import importlib,sys; sys.exit(0 if importlib.util.find_spec('$mod') else 1)" 2>/dev/null; then
    echo "[start.sh] Module '$mod' not importable in venv"
    MISSING+=("$mod")
  else
    echo "[start.sh] Module '$mod' present"
  fi
done

if [ ${#MISSING[@]} -ne 0 ]; then
  echo "[start.sh] Missing critical modules: ${MISSING[*]}"
  if [ "$AUTO_INSTALL" = "1" ]; then
    echo "[start.sh] AUTO_INSTALL=1 - attempting to install missing modules via pip"
    "$VENV_PYTHON" -m pip install "${MISSING[@]}" || echo "[start.sh] Failed to install some missing modules (continuing)" >&2
  else
    echo "[start.sh] To install missing modules automatically, re-run with AUTO_INSTALL=1"
    echo "[start.sh] Or manually run: $VENV_PYTHON -m pip install ${MISSING[*]}"
  fi
fi

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

