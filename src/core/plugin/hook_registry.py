"""Plugin hook registry for CodingAgent (Gap 3).

Provides a lightweight, synchronous hook system that lets users extend
CodingAgent behaviour at well-defined call-sites without modifying core code.

Design principles
-----------------
- **Additive only**: registering a hook never changes existing behaviour unless
  the hook function explicitly raises or modifies a mutable argument.
- **Zero overhead when empty**: ``call()`` checks whether any handlers exist
  before iterating — no cost in the default (unextended) configuration.
- **Fail-safe**: hook exceptions are caught and logged; they never propagate to
  the caller unless ``raise_on_error=True`` is set.
- **Thread-safe**: ``register()`` / ``unregister()`` acquire a lock; ``call()``
  takes a snapshot of handlers to avoid holding the lock during execution.

Hook name constants
-------------------
``HOOK_CONTEXT_BUILT``
    Called after :class:`~src.core.context.context_builder.ContextBuilder`
    produces its output.  Payload: ``{"context": str, "working_dir": str}``.

``HOOK_TOOL_RESULT``
    Called after a tool returns a result.  Payload:
    ``{"tool_name": str, "args": dict, "result": Any}``.

``HOOK_LLM_RESPONSE``
    Called after a raw LLM response is received.  Payload:
    ``{"content": str, "model": str, "round": int}``.

``HOOK_ROUND_END``
    Called at the end of each perception round before state is merged.
    Payload: ``{"round": int, "next_action": str | None}``.

``HOOK_SESSION_START``
    Called once when a new agent session begins.  Payload:
    ``{"session_id": str, "task": str}``.

Usage
-----
::

    from src.core.plugin import registry, HOOK_CONTEXT_BUILT

    def my_hook(payload):
        print("context built:", payload["context"][:80])

    registry.register(HOOK_CONTEXT_BUILT, my_hook)

    # Later, to remove it:
    registry.unregister(HOOK_CONTEXT_BUILT, my_hook)
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hook name constants
# ---------------------------------------------------------------------------

HOOK_CONTEXT_BUILT: str = "context.built"
"""Fired after ContextBuilder produces its output."""

HOOK_TOOL_RESULT: str = "tool.result"
"""Fired after a tool call completes (success or error)."""

HOOK_LLM_RESPONSE: str = "llm.response"
"""Fired after a raw LLM response is received."""

HOOK_ROUND_END: str = "round.end"
"""Fired at the end of each perception round."""

HOOK_SESSION_START: str = "session.start"
"""Fired once when an agent session begins."""


# ---------------------------------------------------------------------------
# HookRegistry
# ---------------------------------------------------------------------------


class HookRegistry:
    """Registry that maps hook names to lists of handler callables.

    All public methods are thread-safe.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        # Dict[hook_name, List[Callable]]
        self._handlers: Dict[str, List[Callable[..., Any]]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, hook: str, fn: Callable[..., Any]) -> None:
        """Register *fn* as a handler for *hook*.

        Registering the same function twice for the same hook results in it
        being called twice; use :meth:`unregister` if that is not desired.

        Args:
            hook: Hook name (use the ``HOOK_*`` constants).
            fn:   Callable that accepts a single ``payload`` dict argument.
        """
        if not callable(fn):
            raise TypeError(f"hook handler must be callable, got {type(fn)!r}")
        with self._lock:
            self._handlers[hook].append(fn)
        _logger.debug("hook_registry: registered %r for hook %r", fn, hook)

    def unregister(self, hook: str, fn: Callable[..., Any]) -> bool:
        """Remove *fn* from the handler list for *hook*.

        Args:
            hook: Hook name.
            fn:   The exact callable previously passed to :meth:`register`.

        Returns:
            ``True`` if the handler was found and removed, ``False`` otherwise.
        """
        with self._lock:
            handlers = self._handlers.get(hook)
            if handlers is None:
                return False
            try:
                handlers.remove(fn)
                _logger.debug("hook_registry: unregistered %r from hook %r", fn, hook)
                return True
            except ValueError:
                return False

    def clear(self, hook: str | None = None) -> None:
        """Remove all handlers for *hook*, or all handlers for all hooks if
        *hook* is ``None``.

        Useful in tests to reset global state between test cases.
        """
        with self._lock:
            if hook is None:
                self._handlers.clear()
            else:
                self._handlers.pop(hook, None)

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def call(
        self,
        hook: str,
        payload: Dict[str, Any] | None = None,
        *,
        raise_on_error: bool = False,
    ) -> None:
        """Invoke all handlers registered for *hook*.

        Handlers are called in registration order.  If a handler raises an
        exception it is logged and execution continues with the next handler
        unless *raise_on_error* is ``True``.

        Args:
            hook:           Hook name.
            payload:        Dict passed as the sole positional argument to each
                            handler.  Defaults to an empty dict.
            raise_on_error: When ``True`` the first exception is re-raised
                            after being logged.
        """
        # Snapshot under lock so we don't hold the lock during execution.
        with self._lock:
            handlers = list(self._handlers.get(hook, []))

        if not handlers:
            return  # fast-path: zero overhead

        if payload is None:
            payload = {}

        for fn in handlers:
            try:
                fn(payload)
            except Exception as exc:
                # Preserve WARNING severity but avoid exc_info=True. Include
                # the formatted traceback in the message for observability.
                try:
                    import traceback

                    _logger.warning(
                        "hook_registry: handler %r raised for hook %r: %s\n%s",
                        fn,
                        hook,
                        exc,
                        traceback.format_exc(),
                    )
                except Exception:
                    _logger.warning(
                        "hook_registry: handler %r raised for hook %r: %s",
                        fn,
                        hook,
                        exc,
                    )
                if raise_on_error:
                    raise

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def handler_count(self, hook: str) -> int:
        """Return the number of handlers registered for *hook*."""
        with self._lock:
            return len(self._handlers.get(hook, []))

    def hooks(self) -> List[str]:
        """Return a sorted list of hook names that have at least one handler."""
        with self._lock:
            return sorted(k for k, v in self._handlers.items() if v)


# ---------------------------------------------------------------------------
# Process-global singleton
# ---------------------------------------------------------------------------

#: The global :class:`HookRegistry` instance.  Import and use this directly::
#:
#:     from src.core.plugin.hook_registry import registry, HOOK_CONTEXT_BUILT
#:     registry.register(HOOK_CONTEXT_BUILT, my_fn)
registry: HookRegistry = HookRegistry()
