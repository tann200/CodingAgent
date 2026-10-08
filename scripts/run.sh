#!/usr/bin/env bash
set -euo pipefail
# Canonical CodingAgent entrypoint — the ONLY supported launcher.
#
# Usage:
#   ./scripts/run.sh --help
#   ./scripts/run.sh --tui
#   ./scripts/run.sh --task "list files"
#
# Requires `uv` (https://docs.astral.sh/uv/); it creates/refreshes `.venv` and
# installs the project on first run.  The deprecated start.sh / starth.sh /
# start_tui.sh / start.ps1 wrappers were removed in P4.4.
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  echo "error: 'uv' is required. Install it: https://docs.astral.sh/uv/#installation" >&2
  exit 1
fi
exec uv run python -m src.main "$@"
