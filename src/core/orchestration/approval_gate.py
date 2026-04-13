"""
approval_gate.py — Non-polling asyncio-based interactive approval gate (S7-B).

Upgraded from ``threading.Event`` to ``asyncio.Event`` so that permission gates
do **not** block the event loop thread.  The previous implementation used
``threading.Event.wait(timeout=120)`` which kept the executor thread alive but
idle, wasting resources when many concurrent permission requests were pending.

Design
------
``AsyncGate`` is a thin wrapper around ``asyncio.Event`` that remembers the
event loop on which it was created.  It exposes two wait methods:

* ``await gate.wait_async(timeout)`` — use from async code that is already
  running inside the same event loop (e.g. orchestrator coroutines).
* ``gate.wait(timeout)`` — use from synchronous code on a ThreadPoolExecutor
  thread (e.g. inside ``bash()`` in file_tools.py, which runs via
  ``asyncio.run()`` in a worker thread).  This method submits a coroutine to
  the gate's loop via ``asyncio.run_coroutine_threadsafe`` and blocks the
  *calling thread* while keeping the event loop free to process other tasks.

Resolving a gate (``resolve_bash_gate``, ``resolve_tool_gate``) uses
``loop.call_soon_threadsafe(event.set)`` so it is safe to call from any thread
or asyncio task.

Backwards-compatible public API
--------------------------------
    register_bash_gate(tool_id)  -> AsyncGate
    resolve_bash_gate(tool_id, approved)
    register_tool_gate(tool_id)  -> AsyncGate
    resolve_tool_gate(tool_id, approved)
    is_bash_denied(tool_id) -> bool
    is_tool_denied(tool_id) -> bool

``AsyncGate`` also implements a ``wait(timeout)`` method that behaves like the
old ``threading.Event.wait(timeout)`` so existing callers need no changes
(they just call ``gate.wait(120.0)`` as before).

Internal state
--------------
    _pending_bash : dict[str, AsyncGate]
    _bash_denied  : set[str]
    _pending_tool : dict[str, AsyncGate]
    _tool_denied  : set[str]
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_pending_bash: dict[str, "AsyncGate"] = {}
_bash_denied: set[str] = set()

_pending_tool: dict[str, "AsyncGate"] = {}
_tool_denied: set[str] = set()

# Protects all four module-level dicts/sets above against concurrent access
# from tool executor threads and TUI / EventBus handler threads.
_gate_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# AsyncGate
# ---------------------------------------------------------------------------


class AsyncGate:
    """Non-polling permission gate backed by ``asyncio.Event``.

    Created on whichever thread/coroutine calls ``register_*_gate()``.
    The loop is captured at construction time; all subsequent operations
    are loop-safe.

    Parameters
    ----------
    loop:
        The running asyncio event loop.  If ``None``, the gate falls back
        to a ``threading.Event`` for environments that do not have a running
        loop (e.g. synchronous unit tests).
    """

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = loop
        if loop is not None:
            self._async_event: asyncio.Event = asyncio.Event()
            self._sync_event = None
        else:
            import threading

            self._async_event = None  # type: ignore[assignment]
            self._sync_event = threading.Event()

    # ------------------------------------------------------------------
    # Async API
    # ------------------------------------------------------------------

    async def wait_async(self, timeout: float = 120.0) -> bool:
        """Await the gate, respecting *timeout* seconds.

        Returns ``True`` if signalled in time, ``False`` on timeout.
        """
        if self._async_event is None:
            # Fallback: sync event wrapped in thread executor
            import asyncio as _asyncio

            loop = _asyncio.get_running_loop()

            ev = self._sync_event
            if ev is None:
                raise RuntimeError("ApprovalGate._sync_event is None in fallback path")

            try:
                # Prefer the central helper which copies the current context
                # and runs the callable in the executor so ContextVars
                # (eg. correlation id) propagate into the worker thread.
                try:
                    from src.core.orchestration.event_bus import run_with_correlation

                    await _asyncio.wait_for(
                        run_with_correlation(loop, None, lambda: ev.wait(timeout)),
                        timeout=timeout + 1,
                    )
                except Exception:
                    # Best-effort fallback when the helper isn't importable
                    # (circular import risk): copy the current context and
                    # use loop.run_in_executor with ctx.run.
                    import contextvars as _contextvars

                    ctx = _contextvars.copy_context()

                    # Wrap ctx.run in a zero-argument callable so run_in_executor
                    # receives a single callable; this avoids typechecker/LSP
                    # complaints about variadic call signatures.
                    def _wait_in_thread() -> bool:
                        return ctx.run(lambda: ev.wait(timeout))

                    await _asyncio.wait_for(
                        loop.run_in_executor(None, _wait_in_thread),
                        timeout=timeout + 1,
                    )
                return ev.is_set()
            except _asyncio.TimeoutError:
                return False
        try:
            await asyncio.wait_for(self._async_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.debug("AsyncGate: timed out after %.1fs", timeout)
            return False

    # ------------------------------------------------------------------
    # Sync API (for ThreadPoolExecutor threads)
    # ------------------------------------------------------------------

    def wait(self, timeout: float = 120.0) -> bool:
        """Block the *calling thread* until the gate is set or *timeout* elapses.

        Safe to call from any thread.  If the gate has an asyncio event loop,
        submits a coroutine via ``run_coroutine_threadsafe`` so the event loop
        itself is never blocked.  If no loop is available, falls back to a
        plain ``threading.Event.wait()``.

        Returns ``True`` if signalled in time, ``False`` on timeout.
        """
        # Case 1: no loop — plain threading.Event fallback (tests / sync code)
        if self._loop is None or self._async_event is None:
            if self._sync_event is not None:
                return self._sync_event.wait(timeout=timeout)
            return False

        # Case 2: loop exists — submit wait coroutine and block this thread
        import concurrent.futures as _cf

        fut = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(self._async_event.wait(), timeout=timeout),
            self._loop,
        )
        try:
            fut.result(timeout=timeout + 5)
            return True
        except (asyncio.TimeoutError, _cf.TimeoutError):
            return False
        except Exception as exc:
            logger.debug("AsyncGate.wait: error: %s", exc)
            return False

    def set(self) -> None:
        """Signal the gate (resolves any waiting coroutine or thread).

        Thread-safe: uses ``call_soon_threadsafe`` when a loop is available.
        """
        if self._loop is not None and self._async_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._async_event.set)
            except RuntimeError:
                # Loop may have been closed (e.g. during shutdown)
                pass
        elif self._sync_event is not None:
            self._sync_event.set()

    def is_set(self) -> bool:
        if self._async_event is not None:
            return self._async_event.is_set()
        if self._sync_event is not None:
            return self._sync_event.is_set()
        return False


# ---------------------------------------------------------------------------
# Helper: get current loop or None
# ---------------------------------------------------------------------------


def _get_running_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Return the running event loop, or None if not in an async context."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# Bash gate
# ---------------------------------------------------------------------------


def register_bash_gate(tool_id: str) -> AsyncGate:
    """Register a pending bash approval request; return the AsyncGate to wait on."""
    gate = AsyncGate(loop=_get_running_loop())
    with _gate_lock:
        _pending_bash[tool_id] = gate
    return gate


def resolve_bash_gate(tool_id: str, approved: bool) -> None:
    """Resolve a pending bash gate.  Call from EventBus handler on any thread."""
    with _gate_lock:
        if not approved:
            _bash_denied.add(tool_id)
        gate = _pending_bash.pop(tool_id, None)
    if gate is not None:
        gate.set()


def is_bash_denied(tool_id: str) -> bool:
    """Return True if *tool_id* was explicitly denied by the user."""
    with _gate_lock:
        return tool_id in _bash_denied


# ---------------------------------------------------------------------------
# Tool gate
# ---------------------------------------------------------------------------


def register_tool_gate(tool_id: str) -> AsyncGate:
    """Register a pending tool permission request; return the AsyncGate to wait on."""
    gate = AsyncGate(loop=_get_running_loop())
    with _gate_lock:
        _pending_tool[tool_id] = gate
    return gate


def resolve_tool_gate(tool_id: str, approved: bool) -> None:
    """Resolve a pending tool permission gate.  Call from EventBus handler."""
    with _gate_lock:
        if not approved:
            _tool_denied.add(tool_id)
        gate = _pending_tool.pop(tool_id, None)
    if gate is not None:
        gate.set()


def is_tool_denied(tool_id: str) -> bool:
    """Return True if *tool_id* was explicitly denied by the user."""
    with _gate_lock:
        return tool_id in _tool_denied


def discard_tool_denied(tool_id: str) -> None:
    """Remove *tool_id* from the denied set (no-op if not present)."""
    with _gate_lock:
        _tool_denied.discard(tool_id)
