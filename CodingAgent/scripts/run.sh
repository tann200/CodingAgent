#!/usr/bin/env bash
set -euo pipefail
# Canonical CodingAgent entrypoint.  Use instead of start.sh / start_tui.sh.
#
# Usage:
#   ./scripts/run.sh --help
#   ./scripts/run.sh --tui
#   ./scripts/run.sh --task "list files"
#
# Depends on `uv` being installed and the virtual environment being active.
cd "$(dirname "$0")/.."
exec uv run python -m src.main "$@"
