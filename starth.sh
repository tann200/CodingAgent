#!/usr/bin/env bash
# starth.sh - simplified launcher that ensures .venv is used and runs the app via start.sh
# Purpose: provide an always-venv startup entrypoint (requested by user)
set -euo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR" || exit 1
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "[starth.sh] Virtualenv python not found at $VENV_PY. Creating venv..."
  python3 -m venv "$VENV_DIR"
fi
# Delegate to start.sh which handles dependency installation and launching
exec "$SCRIPT_DIR/start.sh" "$@"

