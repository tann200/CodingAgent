"""SessionManager — owns per-task session state extracted from CodingAgentOrchestrator.

This module provides :class:`SessionManager`, which concentrates the session
lifecycle responsibilities that were previously scattered across
``orchestrator.py``:

- Per-task identity (``task_id``)
- Per-task file-access tracking (``read_files``, ``modified_files``)
- Snapshot creation and persistence via ``SessionLifecycleManager``
- Tool-call logging via ``SessionStore``
- Session-state hydration via ``AgentSessionManager``
- Changed-file publication to the event bus

By delegating to ``SessionManager``, ``CodingAgentOrchestrator`` is relieved of
~400 lines of session bookkeeping so each concern can be tested independently.

Usage inside orchestrator::

    self.session_mgr = SessionManager(
        working_dir=self.working_dir,
        session_store=self.session_store,
        lifecycle_manager=self.lifecycle_manager,
        event_bus=self.event_bus,
    )
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Set

from src.core.messaging.event_types import SessionFilesChanged
from src.core.utils.strings import valid_str as _valid_str, extract_str as _extract_str

logger = logging.getLogger(__name__)
guilogger = logging.getLogger("codingagent")

if TYPE_CHECKING:
    from src.core.orchestration.event_bus import EventBus

    from src.core.memory.session_store import SessionStore
    from src.core.orchestration.session_lifecycle import SessionLifecycleManager


def _is_git_repo(path: str) -> bool:
    """Return True if *path* is inside a git repository."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


class SessionManager:
    """Owns per-task session state and lifecycle operations.

    Parameters
    ----------
    working_dir:
        Absolute path to the project working directory.
    session_store:
        Persistent store for tool calls and transcript messages.
    lifecycle_manager:
        Manager for graceful-shutdown hooks and session snapshots.
    event_bus:
        Shared event bus used to publish ``session.files_changed`` events.
    msg_mgr:
        Reference to the orchestrator's ``MessageManager``; used when
        building snapshot state.  May be ``None`` at construction time and
        set later via :attr:`msg_mgr`.
    """

    def __init__(
        self,
        working_dir: Path,
        session_store: "SessionStore",
        lifecycle_manager: "SessionLifecycleManager",
        event_bus: "EventBus",
        msg_mgr: Any = None,
    ) -> None:
        self.working_dir = Path(working_dir)
        self.session_store = session_store
        self.lifecycle_manager = lifecycle_manager
        self.event_bus = event_bus
        self.msg_mgr = msg_mgr  # set after MessageManager construction

        # Per-task state — reset by start_new_task()
        self._task_id: Optional[str] = None
        self._read_files: Set[str] = set()
        self._modified_files: Set[str] = set()

    # ------------------------------------------------------------------
    # Properties — expose state under both the new and legacy names
    # ------------------------------------------------------------------

    @property
    def task_id(self) -> Optional[str]:
        """Current task identifier (short UUID prefix)."""
        return self._task_id

    @task_id.setter
    def task_id(self, value: Optional[str]) -> None:
        self._task_id = value

    @property
    def read_files(self) -> Set[str]:
        """Set of file paths read during the current task."""
        return self._read_files

    @property
    def modified_files(self) -> Set[str]:
        """Set of file paths modified during the current task."""
        return self._modified_files

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def start_new_task(self) -> str:
        """Allocate a new task ID and reset all per-task state.

        Returns the newly generated task ID (8-char UUID prefix).

        This method is the single authoritative place where session state is
        cleared between tasks.  Previously this logic was embedded in
        ``CodingAgentOrchestrator.start_new_task()``.
        """
        self._task_id = str(uuid.uuid4())[:8]
        self._read_files = set()
        self._modified_files = set()
        guilogger.info(f"[SessionManager] New task started: {self._task_id}")
        return self._task_id

    def reset_read_files(self) -> None:
        """Clear the read-file set (called at the start of ``run_agent_once``)."""
        self._read_files = set()

    def record_read(self, path: str) -> None:
        """Mark *path* as having been read in this task."""
        self._read_files.add(path)

    def record_modified(self, path: str) -> None:
        """Mark *path* as having been modified in this task."""
        self._modified_files.add(path)

    # ------------------------------------------------------------------
    # SessionStore delegation
    # ------------------------------------------------------------------

    def log_tool_call(
        self,
        tool_name: str,
        tool_args: dict,
        result: Any,
        *,
        success: bool = True,
    ) -> None:
        """Persist a tool call record to the ``SessionStore``.

        Parameters
        ----------
        tool_name:
            Canonical name of the tool that was invoked.
        tool_args:
            Arguments passed to the tool.
        result:
            The serialisable result returned (or the error string).
        success:
            ``True`` for successful calls, ``False`` for failures.
        """
        try:
            import threading as _thr

            _sid = self._task_id or "unknown"
            _thread_name = getattr(_thr.current_thread(), "name", "unknown")
            guilogger.debug(
                "session_store: write (session=%r, thread=%s, site=%s)",
                _sid,
                _thread_name,
                "SessionManager:log_tool_call",
            )
            self.session_store.add_tool_call(
                session_id=_sid,
                tool_name=tool_name,
                args=tool_args,
                result=result,
                success=success,
            )
        except Exception as exc:
            guilogger.debug(f"[SessionManager] log_tool_call failed: {exc}")

    def log_message(self, role: str, content: str) -> None:
        """Persist a conversation message to the ``SessionStore`` transcript."""
        try:
            import threading as _thr

            _sid = self._task_id or "unknown"
            _thread_name = getattr(_thr.current_thread(), "name", "unknown")
            guilogger.debug(
                "session_store: write (session=%r, thread=%s, site=%s)",
                _sid,
                _thread_name,
                "SessionManager:log_message",
            )
            self.session_store.add_message(
                session_id=_sid,
                role=role,
                content=content,
            )
        except Exception as exc:
            guilogger.debug(f"[SessionManager] log_message failed: {exc}")

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def create_snapshot(self, usage_buffer: Optional[dict] = None) -> None:
        """Create a session snapshot for resume capability.

        Parameters
        ----------
        usage_buffer:
            Optional mapping of ``{tool_name: call_count}`` tracked by the
            orchestrator's legacy ``_usage_buffer``.  Used to populate
            ``tool_call_count`` in the snapshot state.
        """
        if not self.lifecycle_manager or not self._task_id:
            return
        try:
            # BUG-FIX: check messages attribute exists
            history: List[dict] = []
            if self.msg_mgr and hasattr(self.msg_mgr, "messages"):
                history = list(self.msg_mgr.messages)
            tool_call_count = sum(usage_buffer.values()) if usage_buffer else 0
            state: dict[str, Any] = {
                "task": "",
                "history": history,
                "current_step": 0,
                "current_plan": None,
                "verified_reads": list(self._read_files),
                "files_read": {},
                "tool_call_count": tool_call_count,
                "rounds": 0,
                "session_id": self._task_id,
            }
            first_msg = (
                history[0].get("content", "")[:100] if history and history[0] else ""
            )
            snapshot = self.lifecycle_manager.create_snapshot(
                session_id=self._task_id,
                state=state,
                metadata={"task_description": first_msg},
            )
            self.lifecycle_manager.save_snapshot(snapshot)
            guilogger.info(
                f"[SessionManager] Created snapshot for task: {self._task_id}"
            )
        except Exception as exc:
            guilogger.warning(f"[SessionManager] create_snapshot failed: {exc}")

    # ------------------------------------------------------------------
    # Event bus publication
    # ------------------------------------------------------------------

    def publish_files_changed(self) -> None:
        """Publish a ``session.files_changed`` event for sidebar display.

        Does nothing if no files were modified in this task.
        """
        try:
            if not self._modified_files:
                return
            workdir_path = self.working_dir.resolve()
            changes = []
            for f in self._modified_files:
                try:
                    fpath = Path(f).resolve()
                    if str(fpath).startswith(str(workdir_path)):
                        rel_path = str(fpath.relative_to(workdir_path))
                    else:
                        rel_path = str(fpath)
                    changes.append({"path": rel_path, "absolute": str(fpath)})
                except Exception:
                    changes.append({"path": str(f), "absolute": str(f)})

            self.event_bus.publish_typed(SessionFilesChanged(files=changes, workdir=str(workdir_path), is_git_repo=_is_git_repo(str(workdir_path))))
        except Exception as exc:
            logger.warning(
                "session.files_changed event dropped (non-fatal): %s", exc
            )

    # ------------------------------------------------------------------
    # AgentSessionManager hydration
    # ------------------------------------------------------------------

    def sync_agent_session_state(
        self,
        adapter: Any = None,
        task: str = "",
        current_plan: Optional[list] = None,
        current_step: int = 0,
    ) -> None:
        """Push current state into ``AgentSessionManager`` for TUI hydration.

        Parameters
        ----------
        adapter:
            The LLM adapter instance (used to extract provider/model names).
        task:
            Human-readable task description string.
        current_plan / current_step:
            Current plan steps and active step index.
        """
        try:
            from src.core.orchestration.agent_session_manager import (
                get_agent_session_manager,
            )

            provider_name = ""
            model_name = ""

            # If caller did not provide an adapter, attempt a conservative
            # fallback to ProviderManager.get_active_adapter() so callers that
            # rely on the active provider can omit passing an adapter. Keep
            # this best-effort and never raise if imports or calls fail.
            if adapter is None:
                try:
                    from src.core.inference.llm_manager import get_provider_manager

                    pm = get_provider_manager()
                    if pm is not None:
                        try:
                            adapter = pm.get_active_adapter()
                        except Exception:
                            adapter = None
                except Exception:
                    adapter = None

            # Use centralised resolution pattern to obtain provider/model names
            if adapter is not None:
                caps = None
                try:
                    from src.core.inference.llm_manager import get_provider_manager

                    pm = get_provider_manager()
                    if pm is not None:
                        try:
                            caps = pm.get_provider_capabilities(adapter)
                        except Exception:
                            caps = None
                except Exception:
                    caps = None

                if isinstance(caps, dict):
                    try:
                        p_cand = caps.get("provider_name") or caps.get("provider")
                        p_cand = _extract_str(p_cand)
                        if p_cand and _valid_str(p_cand):
                            provider_name = p_cand
                        m_cand = caps.get("model") or caps.get("default_model")
                        m_cand = _extract_str(m_cand)
                        if m_cand and _valid_str(m_cand):
                            model_name = m_cand
                    except Exception:
                        # keep existing values on error
                        pass

                # If ProviderManager didn't yield concrete values, inspect adapter
                if not _valid_str(provider_name) or not _valid_str(model_name):
                    try:
                        p = getattr(adapter, "provider", None)
                        p_cand = _extract_str(p)
                        if (
                            p_cand
                            and _valid_str(p_cand)
                            and not _valid_str(provider_name)
                        ):
                            provider_name = p_cand

                        dm = _extract_str(getattr(adapter, "default_model", None))
                        if dm and _valid_str(dm):
                            model_name = dm
                        else:
                            ms = getattr(adapter, "models", None)
                            if isinstance(ms, list):
                                for m in ms:
                                    m_c = _extract_str(m)
                                    if m_c and _valid_str(m_c):
                                        model_name = m_c
                                        break
                    except Exception:
                        pass

            session_mgr = get_agent_session_manager()
            session_mgr.update_session_state(
                session_id=self._task_id or "default",
                task=task,
                message_history=(
                    list(self.msg_mgr.messages)
                    if self.msg_mgr and hasattr(self.msg_mgr, "messages")
                    else []
                ),
                current_plan=current_plan or [],
                current_step=current_step,
                provider=provider_name,
                model=model_name,
                files_read=list(self._read_files),
                files_modified=list(self._modified_files),
            )
        except Exception as exc:
            guilogger.debug(f"[SessionManager] sync_agent_session_state failed: {exc}")
