"""preview_coordinator.py — D-10: Extracted diff-preview coordination service.

Owns the TUI-05 blocking diff-preview gate: subscribes to
``preview.confirmed`` / ``preview.rejected`` EventBus events and resolves
the corresponding threading.Event gates in ``file_tools``.

``Orchestrator`` creates one ``PreviewCoordinator`` at init and calls
``coordinator.attach(event_bus)`` to wire the subscriptions.

Usage::

    coordinator = PreviewCoordinator()
    coordinator.attach(event_bus)

    # In file_tools.write_file (or edit_file_atomic):
    gate = coordinator.register_gate(path_key)
    # … publish diff preview event …
    gate.wait(timeout=300.0)
    if coordinator.was_rejected(path_key):
        return {"status": "rejected"}
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PreviewCoordinator:
    """Coordinates blocking diff-preview gates between file_tools and the TUI.

    Responsibilities
    ----------------
    - Subscribe to ``preview.confirmed`` / ``preview.rejected`` events on the
      EventBus and resolve the corresponding gate in ``file_tools``.
    - Expose ``register_gate()``, ``resolve_gate()``, and ``was_rejected()``
      so that tool functions can block pending user review without importing
      Orchestrator directly.

    The gate state is maintained in ``file_tools`` (threading.Event registry)
    to avoid a circular import.  ``PreviewCoordinator`` is the single point
    that connects EventBus events to those gates.
    """

    def __init__(self) -> None:
        self._attached: bool = False
        self._event_bus: Optional[Any] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self, event_bus: Any) -> None:
        """Subscribe to ``preview.confirmed`` / ``preview.rejected`` events.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._attached:
            return
        self._event_bus = event_bus
        try:
            event_bus.subscribe("preview.confirmed", self._on_confirmed)
            event_bus.subscribe("preview.rejected", self._on_rejected)
            self._attached = True
            logger.debug("PreviewCoordinator: attached to event bus")
        except Exception as exc:
            logger.debug("PreviewCoordinator: attach error: %s", exc)

    def detach(self) -> None:
        """Unsubscribe from EventBus events."""
        if not self._attached or self._event_bus is None:
            return
        try:
            self._event_bus.unsubscribe("preview.confirmed", self._on_confirmed)
            self._event_bus.unsubscribe("preview.rejected", self._on_rejected)
        except Exception:
            pass
        self._attached = False

    # ------------------------------------------------------------------
    # Internal event handlers
    # ------------------------------------------------------------------

    def _on_confirmed(self, payload: dict) -> None:
        path = str(payload.get("path") or "").strip()
        if not path:
            logger.warning(
                "PreviewCoordinator: 'preview.confirmed' event missing valid 'path' key"
            )
            return
        try:
            from src.tools.file_tools import resolve_preview_gate  # type: ignore[import]

            resolve_preview_gate(path, approved=True)
        except Exception as exc:
            logger.debug("PreviewCoordinator: confirm error: %s", exc)

    def _on_rejected(self, payload: dict) -> None:
        path = str(payload.get("path") or "").strip()
        if not path:
            logger.warning(
                "PreviewCoordinator: 'preview.rejected' event missing valid 'path' key"
            )
            return
        try:
            from src.tools.file_tools import resolve_preview_gate  # type: ignore[import]

            resolve_preview_gate(path, approved=False)
        except Exception as exc:
            logger.debug("PreviewCoordinator: reject error: %s", exc)
