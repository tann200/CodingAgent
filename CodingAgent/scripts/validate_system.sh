#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
PYTHON_BIN=${PYTHON:-python3}

cd "$ROOT_DIR"

exec "$PYTHON_BIN" -m pytest tests/acceptance/test_system_validation.py -q -p no:logging "$@"
