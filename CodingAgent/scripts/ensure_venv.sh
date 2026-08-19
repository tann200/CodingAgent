#!/usr/bin/env bash
set -euo pipefail

VENV_DIR=".venv"
PYTHON=${PYTHON:-python3}

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtualenv in $VENV_DIR"
  $PYTHON -m venv "$VENV_DIR"
fi

# Activate and upgrade pip
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip setuptools wheel

# Install pinned requirements
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
else
  echo "requirements.txt not found; skipping install"
fi

echo "Virtualenv ready. Activate with: source $VENV_DIR/bin/activate"
