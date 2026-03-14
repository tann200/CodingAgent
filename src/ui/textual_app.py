"""Minimal Textual app adapter shim (optional).

This module provides a placeholder `TextualApp` class that will be implemented
later. It imports Textual only when run, so importing this file does not require
Textual to be installed (tests can still import it safely).
"""
from __future__ import annotations

from typing import Optional

class TextualApp:
    def __init__(self, controllers: Optional[dict] = None):
        self.controllers = controllers or {}
        self.running = False

    def run(self) -> None:
        try:
            # import textual only when running
            pass
        except Exception:
            # Textual not installed; fallback to no-op but provide informative message
            print("Textual is not installed; TextualApp.run is a no-op in headless mode")
            return
        # Real implementation will create an App subclass and mount widgets
        print("TextualApp.run: launching textual UI (not implemented)")

