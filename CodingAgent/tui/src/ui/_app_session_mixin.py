"""AppSessionMixin — session lifecycle methods for AgentApp.

Extracted from ``tui/src/ui/app.py`` (lines 676–816) to reduce AgentApp
to a ≤400-line core.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from textual.widgets import Static

from .logging import get_logger

from ._app_protocol import AgentAppProtocol

logger = get_logger("app_session")

class AppSessionMixin(AgentAppProtocol):
    """Session lifecycle helpers — snapshot, new session, sidebar reset.

    Expects the host class to expose:
    - ``self._bridge`` (AgentBridge instance)
    - ``self._session_id`` (str)
    - ``self._modified_files`` (list[str])
    - ``self.total_tokens``, ``self.pending_tasks``, ``self.queue_size`` (reactive ints)
    - ``self._tool_call_count``, ``self._session_input_tokens``,
      ``self._session_output_tokens`` (int)
    - ``self.query_one(selector, type)`` — Textual standard
    - ``self._update_status_bar()`` — from StatusBarMixin
    - ``self._clear_chat_panel()`` — from ChatDisplayMixin
    """

    # ── Session snapshot ──────────────────────────────────────────────────

    def _get_sessions_dir(self: AgentAppProtocol) -> Path:
        # Cross-platform sessions directory — prefer core.paths.get_sessions_dir()
        from ._core_paths_loader import get_sessions_dir as _get_sessions_dir_helper

        d = _get_sessions_dir_helper()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_session_snapshot(self: AgentAppProtocol) -> None:
        """Snapshot current session to the sessions directory returned by
        ``src.core.paths.get_sessions_dir()`` (fallback to ~/.coding_agent/sessions
        in TUI dev mode).

        TASK-05: enriched payload includes version, session_id, turn_count,
        input_tokens, output_tokens for SessionListScreen + resumption.
        """
        import json
        import tempfile

        try:
            with self._bridge._history_lock:
                history = list(self._bridge.history)
            if not history:
                return
            first_user = next(
                (text[:60] for role, text in history if role == "user"), ""
            )
            # TASK-05: pull turn count and token totals from bridge accessors
            turn_count = self._bridge.get_turn_count()
            input_tokens, output_tokens = self._bridge.get_usage_totals()
            created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            payload = {
                "version": 1,
                "session_id": self._session_id,
                "timestamp": time.time(),
                "created_at": created_at,
                "task_name": first_user,
                "message_count": len(history),
                "turn_count": turn_count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "messages": [{"role": r, "content": t} for r, t in history],
                "working_dir": self._bridge.working_dir or str(os.getcwd()),
            }
            p = self._get_sessions_dir() / f"session_{self._session_id}.json"

            # Ensure parent dir immediately before writing
            p.parent.mkdir(parents=True, exist_ok=True)

            # Prefer central atomic writer; fall back to mkstemp+replace
            try:
                from src.core.io_utils import atomic_write_json

                logger.debug("app: attempting atomic_write_json for %s", p)
                ok = atomic_write_json(p, payload, logger=logger)
                if ok:
                    logger.info("Session snapshot written atomically: %s", p)
                    return
                logger.warning(
                    "app: atomic_write_json returned False for %s; falling back",
                    p,
                )
            except Exception as _e:
                import traceback as _traceback

                logger.debug(
                    "app: atomic_write_json unavailable or failed for %s; falling back: %s\n%s",
                    p,
                    _e,
                    _traceback.format_exc(),
                )

            fd = None
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
                try:
                    try:
                        fobj = os.fdopen(fd, "w", encoding="utf-8")
                    except Exception:
                        if fd is not None:
                            os.close(fd)
                        raise
                    with fobj:
                        json.dump(payload, fobj, ensure_ascii=False)
                        try:
                            fobj.flush()
                            os.fsync(fobj.fileno())
                        except Exception:
                            pass
                    os.replace(tmp_path, str(p))
                except Exception:
                    try:
                        if fd is not None:
                            os.close(fd)
                    except Exception:
                        pass
                    raise
            except Exception as e:
                logger.error("Session snapshot save failed: %s", e)
                try:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception:
            pass

    # ── §10.3 New session ─────────────────────────────────────────────────

    def _handle_session_new(self: AgentAppProtocol) -> None:
        """Called from bridge when session.new fires."""
        self._clear_chat_panel()
        self._reset_sidebar()

    def _reset_sidebar(self: AgentAppProtocol) -> None:
        self._modified_files.clear()
        self.total_tokens = 0
        self.pending_tasks = 0
        self.queue_size = 0
        self._tool_call_count = 0
        self._session_input_tokens = 0
        self._session_output_tokens = 0
        try:
            self.query_one("#sb_task_status", Static).update("idle")
            self.query_one("#sb_plan_bar", Static).update("—")
            self.query_one("#sb_plan_desc", Static).update("")
            self.query_one("#sb_tool_activity", Static).update("—")
            self.query_one("#sb_session", Static).update("Pending: 0 | Queue: 0")
            self.query_one("#sb_tokens", Static).update("0 / 32,000  (0.0%)")
            self.query_one("#sb_context", Static).update("In: 0 | Out: 0")
            self.query_one("#sb_cost", Static).update("$0.000")
            self.query_one("#sb_git", Static).update("○ —")
            self.query_one("#sb_tool_count", Static).update("0")
            self.query_one("#sb_files", Static).update("None")
            self.query_one("#sb_status", Static).update("Status: idle")
            self._update_status_bar()
        except Exception as e:
            logger.error(f"Error resetting sidebar: {e}")
