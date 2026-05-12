"""abstract_session_store.py — Formal abstract base for session store implementations.

Provides:
- ``AbstractSessionStore``: runtime-checkable Protocol that both
  ``SqliteSessionStore`` and ``JsonlSessionStore`` satisfy structurally.
  This is a re-export of ``SessionStoreProtocol`` under the name used in
  the refactoring plan so all new code can ``from abstract_session_store
  import AbstractSessionStore`` without touching the canonical definition
  in ``src/core/interfaces.py``.

- ``create_session_store(workdir, backend) -> AbstractSessionStore``:
  public factory alias for ``get_session_store`` that returns a concrete
  implementation typed as ``AbstractSessionStore``.  Preferred entry-point
  for new code; ``get_session_store`` remains for backwards compatibility.
"""

from __future__ import annotations

from typing import Optional

# Re-export the canonical protocol under the plan-agreed name.
from src.core.interfaces import SessionStoreProtocol as AbstractSessionStore


def create_session_store(
    workdir: Optional[str] = None,
    backend: Optional[str] = None,
) -> AbstractSessionStore:
    """Factory: return a concrete ``AbstractSessionStore`` implementation.

    Parameters
    ----------
    workdir:
        Project root directory.  Passed through to the chosen backend.
    backend:
        ``"jsonl"`` (default), ``"sqlite"``, or ``None`` to use the
        configured default.

    Returns
    -------
    AbstractSessionStore
        A fully initialised session store instance.
    """
    from src.core.memory.session_store import get_session_store

    return get_session_store(workdir=workdir, backend=backend)  # type: ignore[return-value]


__all__ = ["AbstractSessionStore", "create_session_store"]
