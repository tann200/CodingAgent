#!/usr/bin/env bash
# CodingAgent — canonical launch script.
#
# Usage:
#   scripts/run.sh [--tui] [--headless] [--task "..."] [any other args]
#
# Preferred: use `uv run codingagent [args]` when uv is available.
# This script is a fallback that activates the local venv then delegates.
#
# Examples:
#   scripts/run.sh --tui
#   scripts/run.sh --task "list all Python files"
#   uv run codingagent --tui

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Activate virtual environment if present and not already active
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

exec python -m src.main "$@"
