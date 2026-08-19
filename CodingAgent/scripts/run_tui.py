"""Simple runner for the Textual TUI implementation.

Usage:
    LM_STUDIO_URL=http://localhost:1234/v1 .venv/bin/python3 scripts/run_tui.py

If Textual is not installed the script will print instructions to install it.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so `tui.src.ui.main` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tui.src.ui.main import create_app  # type: ignore[import]


def main():
    app = create_app()
    if hasattr(app, "run"):
        app.run()
    else:
        print("App has no run() method; Textual likely not installed.")


if __name__ == "__main__":
    main()
