#!/usr/bin/env python3
"""Start the CodingAgent application (Textual TUI preferred).

Usage: python scripts/start_tui.py [args passed to main]

This runner sets PYTHONPATH to the project root so local imports work when
invoked from the repo root.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('PYTHONPATH', str(ROOT))
sys.path.insert(0, str(ROOT))

from main import main

if __name__ == '__main__':
    rc = main(sys.argv[1:])
    raise SystemExit(rc)

