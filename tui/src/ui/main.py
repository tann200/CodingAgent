"""
tui/src/ui/main.py — entry-point factory for the standalone TUI.

Usage:
    from tui.src.ui.main import create_app
    app = create_app()
    app.run()

Or run directly:
    python -m tui.src.ui.main
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .app import AgentApp


def create_app() -> "AgentApp":
    """Instantiate and return an AgentApp ready to run.

    Import is deferred so that importing this module does not pull in
    Textual unless the TUI is actually being started.
    """
    from .app import AgentApp

    return AgentApp()


class TextualAppStub:
    """Headless stub used in tests or when Textual is unavailable.

    Provides the same surface as AgentApp's public send/interrupt API so
    that callers can always depend on this module regardless of whether the
    real TUI is available.
    """

    def __init__(self) -> None:
        self._messages: list[tuple[str, str]] = []

    def send_prompt(self, text: str) -> bool:
        """Accept a prompt and store it; returns True."""
        self._messages.append(("user", text))
        return True

    def interrupt(self) -> None:
        """No-op interrupt for the stub."""

    def force_interrupt(self) -> None:
        """No-op force-interrupt for the stub."""

    def run(
        self,
        *,
        headless: bool = False,
        inline: bool = False,
        mouse: bool = True,
        size: Optional[tuple[int, int]] = None,
        **kwargs: object,
    ) -> None:
        """No-op run; stub cannot actually render a TUI."""


def _get_app(headless: bool = False) -> "AgentApp | TextualAppStub":
    """Return a real AgentApp when Textual is available, else a stub."""
    try:
        return create_app()
    except Exception:
        return TextualAppStub()


if __name__ == "__main__":
    app = create_app()
    app.run()
