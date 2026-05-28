"""BridgeSessionMixin — history persistence and session lifecycle methods.

Contains: load_history, _save_history, save_history, clear_history,
undo_last_user_message, _get_prompt_history_path, load_prompt_history,
update_prompt_history, publish_session_request, publish_session_new,
start_new_session, restore_and_continue.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ._bridge_protocol import AgentBridgeProtocol
from ._core_paths_loader import get_data_dir
from .logging import get_logger

logger = get_logger("bridge")

if TYPE_CHECKING:
    pass

_AGENT_DIR = get_data_dir()
HISTORY_PATH = _AGENT_DIR / "tui_conversation_history.json"


class BridgeSessionMixin(AgentBridgeProtocol):
    """Mixin providing history persistence and session lifecycle methods."""

    # SES-W1: versioned history envelope.  Version 1 wraps the list in a dict
    # so future format changes can be detected and migrated at load time.
    _HISTORY_VERSION = 1

    def load_history(self) -> None:
        """Atomic load on startup (§15.3).

        Supports both the legacy bare-list format (v0) and the current versioned
        envelope format (v1: {"version": 1, "history": [...]}).
        """
        if not HISTORY_PATH.exists():
            return
        try:
            raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            # v1 versioned envelope
            if isinstance(raw, dict) and "history" in raw:
                entries = raw["history"]
            # v0 legacy bare list — migrate transparently
            elif isinstance(raw, list):
                entries = raw
            else:
                entries = []
            with self._history_lock:
                self.history = [
                    tuple(item)
                    for item in entries
                    if isinstance(item, (list, tuple)) and len(item) == 2
                ]
            logger.info(f"History loaded: {len(self.history)} entries")
        except Exception as e:
            logger.warning(f"History load failed (starting fresh): {e}")

    def _save_history(self) -> None:
        """Atomic write after every agent result (§15.4)."""
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Snapshot history under the lock, then attempt centralized atomic write
        with self._history_lock:
            payload = {"version": self._HISTORY_VERSION, "history": list(self.history)}

        try:
            from src.core.io_utils import atomic_write_json

            logger.debug("bridge: attempting atomic_write_json for %s", HISTORY_PATH)
            ok = atomic_write_json(HISTORY_PATH, payload, logger=logger)
            if ok:
                logger.info("History written atomically: %s", HISTORY_PATH)
                return
            logger.warning(
                "bridge: atomic_write_json returned False for %s; falling back",
                HISTORY_PATH,
            )
        except Exception as _e:
            import traceback as _traceback

            logger.debug(
                "bridge: atomic_write_json unavailable or failed for %s; falling back: %s\n%s",
                HISTORY_PATH,
                _e,
                _traceback.format_exc(),
            )

        # Fallback to legacy temp-file write
        fd, tmp = tempfile.mkstemp(dir=str(HISTORY_PATH.parent), suffix=".tmp")
        try:
            try:
                fobj = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                os.close(fd)
                raise
            with fobj:
                json.dump(payload, fobj, ensure_ascii=False, indent=2)
            os.replace(tmp, str(HISTORY_PATH))
        except Exception as e:
            logger.error("History save failed: %s", e)
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def save_history(self) -> None:
        """Public save — call at shutdown."""
        self._save_history()

    def clear_history(self) -> None:
        with self._history_lock:
            self.history.clear()
        self._save_history()

    def undo_last_user_message(self) -> bool:
        """Remove the last user message from history (GAP-CMD-1).

        Returns True if a user message was removed, False if no user messages exist.
        """
        with self._history_lock:
            # Find the last user message (working backwards)
            for i in range(len(self.history) - 1, -1, -1):
                hist_entry = self.history[i]
                if (
                    isinstance(hist_entry, (list, tuple))
                    and len(hist_entry) >= 2
                    and hist_entry[0] == "user"
                ):
                    removed = self.history.pop(i)
                    self._save_history()
                    logger.info(f"Undo: removed user message '{removed[1][:50]}...'")
                    return True
            return False

    # ── Frecency-scored prompt history (§H6) ─────────────────────────────

    @staticmethod
    def _get_prompt_history_path() -> Path:
        # Use the agent data dir (prefers src.core.paths.get_data_dir() when
        # available; falls back to Path.home() based legacy dir)
        hist_dir = _AGENT_DIR
        hist_dir.mkdir(parents=True, exist_ok=True)
        return hist_dir / "tui_prompt_history.json"

    def load_prompt_history(self) -> list[str]:
        """Load frecency-scored prompt history.

        Returns list of prompt strings sorted by score (most frequent/recent first).
        Score formula: count / ((1 + hours_ago) ** 0.5). Top 500 entries returned.
        """
        try:
            p = self._get_prompt_history_path()
            if not p.exists():
                return []
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            now = time.time()
            entries: list[tuple[float, str]] = []
            for entry in data:
                if not isinstance(entry, dict) or "text" not in entry:
                    continue
                text: str = str(entry["text"])
                count: int = int(entry.get("count", 1))
                last_used: float = float(entry.get("last_used", now))
                hours_ago: float = max(0.0, (now - last_used) / 3600)
                score: float = count / ((1 + hours_ago) ** 0.5)
                entries.append((score, text))
            entries.sort(reverse=True)
            return [t for _, t in entries[:500]]
        except Exception:
            return []

    def update_prompt_history(self, text: str) -> None:
        """Record a prompt submission, update frecency scores, persist atomically."""
        try:
            p = self._get_prompt_history_path()
            now = time.time()
            data: list[dict] = []
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        data = raw
                except Exception:
                    data = []

            # Update existing entry or insert new one
            found = False
            for entry in data:
                if isinstance(entry, dict) and entry.get("text") == text:
                    entry["count"] = int(entry.get("count", 0)) + 1
                    entry["last_used"] = now
                    found = True
                    break
            if not found:
                data.append({"text": text, "count": 1, "last_used": now})

            # Prune to top 500 by frecency score
            def _score(e: dict) -> float:
                hours_ago = max(0.0, (now - float(e.get("last_used", now))) / 3600)
                return int(e.get("count", 1)) / ((1 + hours_ago) ** 0.5)

            data.sort(key=_score, reverse=True)
            data = data[:500]

            # Prefer central atomic writer; fall back to mkstemp+replace
            try:
                from src.core.io_utils import atomic_write_json

                logger.debug("bridge: attempting atomic_write_json for %s", p)
                ok = atomic_write_json(p, data, logger=logger)
                if ok:
                    logger.debug("bridge: prompt history written atomically: %s", p)
                    return
                logger.warning(
                    "bridge: atomic_write_json returned False for %s; falling back",
                    p,
                )
            except Exception as _e:
                import traceback as _traceback

                logger.debug(
                    "bridge: atomic_write_json unavailable or failed for %s; falling back: %s\n%s",
                    p,
                    _e,
                    _traceback.format_exc(),
                )

            fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp_path, str(p))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:
            pass

    def publish_session_request(self) -> None:
        """Publish session.request_state on startup (§10.1 step 6).

        Also triggers the full startup chain via ``_publish_system_settings()``:
        loads config, fires ``orchestrator.startup`` → ``_on_orchestrator_startup``
        → ``_publish_active_provider_status()`` so the banner and sidebar reflect
        the real provider state immediately instead of staying at
        "connecting…" / "disconnected".
        """
        self._bus.publish("session.request_state", {"session_id": "default"})
        # Kick off the startup chain so the UI status indicators are updated.
        self._publish_system_settings()

    def publish_session_new(self) -> None:
        """Publish session.new on /new command (§10.3)."""
        self._bus.publish("session.new", {"timestamp": time.time()})

    def start_new_session(self) -> None:
        """Public: reset orchestrator task state + publish session.new (§10.3)."""
        if self._orchestrator:
            start_fn = getattr(self._orchestrator, "start_new_task", None)
            if callable(start_fn):
                try:
                    start_fn()
                except Exception as exc:
                    logger.warning(f"start_new_task() failed: {exc}")
        self.publish_session_new()

    def restore_and_continue(
        self, last_task: str, continue_state: Optional[dict]
    ) -> bool:
        """Restore previous state (if any) and re-submit the last task. Returns False if already running."""
        orch = self._orchestrator
        if orch and continue_state:
            restore_fn = getattr(orch, "restore_continue_state", None)
            if callable(restore_fn):
                try:
                    restore_fn(continue_state)
                    logger.info("restore_and_continue: state restored")
                except Exception as exc:
                    logger.warning(f"restore_and_continue: restore failed: {exc}")
        return self.send_prompt(last_task)
