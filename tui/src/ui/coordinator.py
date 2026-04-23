from __future__ import annotations
from typing import Tuple

try:
    # Prefer the absolute import when the test/runtime environment has a
    # top-level `src` package that provides ui.logging.  If that fails (e.g.
    # when running the standalone TUI package as tui.src.*), fall back to a
    # package-relative import.  As a last resort, use the stdlib logging
    # module so the module remains importable in constrained test envs.
    from src.ui.logging import get_logger
except Exception:
    try:
        from .logging import get_logger
    except Exception:
        import logging

        def get_logger(name: str) -> logging.Logger:
            return logging.getLogger(name)


logger = get_logger("coordinator")


class Coordinator:
    def get_sidebar_data(self) -> Tuple[int, int]:
        return 0, 0
