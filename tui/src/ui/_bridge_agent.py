"""BridgeAgentMixin — agent run loop, interrupt, and usage accounting.

Contains: send_prompt, _run_agent, interrupt, force_interrupt,
pop_pending_injections, get_turn_count, get_usage_totals.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from ._bridge_protocol import AgentBridgeProtocol
from .logging import get_logger

logger = get_logger("bridge")

# AUTO-03: Map TUI role names to CodingAgent system prompt names.
TUI_ROLE_TO_PROMPT: dict[str, str] = {
    "lead_architect": "strategic",  # planning, design
    "full_stack_engineer": "operational",  # execution, coding
    "qa_lead": "reviewer",  # review, testing
}


class BridgeAgentMixin(AgentBridgeProtocol):
    """Mixin providing agent execution, interrupt, and usage tracking."""

    def send_prompt(self, text: str) -> bool:
        """Thread-safe prompt submission.

        If the agent is currently running, the message is appended to the
        mid-run injection buffer so perception_node can pick it up on the
        next LLM round (OpenCode-style system-reminder injection).  Returns
        False in that case so the caller knows the message was buffered, not
        immediately dispatched.
        """
        with self._agent_lock:
            if self._agent_running:  # type: ignore[has-type]
                # MID-INJ: buffer for mid-run injection
                self._pending_injections.append(text)
                return False
            self._cancel_event.clear()  # clear inside lock to avoid race with interrupt()
            self._agent_running = True
        # MID-INJ: clear any stale injections from a previous run
        with self._agent_lock:
            self._pending_injections.clear()
        with self._history_lock:
            self.history.append(("user", text))
        pool = getattr(self, "_thread_pool", None)
        if pool is None or pool._shutdown:
            pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bridge")
            self._thread_pool = pool
        pool.submit(self._run_agent, text)
        return True

    def _run_agent(self, text: str) -> None:
        """Run the agent on a background thread (§7.1)."""
        from tui.tui_src.ui.bus import AgentFinalResponse, WorkerError, AgentRunningEvent

        _logger = logger
        try:
            self._post(AgentRunningEvent(running=True))
            if self._orchestrator:
                self._ensure_deferred_init()
                # NOTE: start_new_task() is intentionally NOT called here — it
                # clears msg_mgr.messages, which would wipe conversation history
                # on every follow-up message.  start_new_task() is called only
                # from start_new_session() (triggered by /new).
                # AUTO-02: apply per-role autonomy settings before each run
                try:
                    from src.core.config_loader import (  # type: ignore[import, attr-defined]
                        get_role_config,
                        load_merged_config,
                    )
                    from src.tools.tools_config import (  # type: ignore[import]
                        set_autonomous,
                        set_require_preview_confirmation,
                    )

                    _wdir = Path(self._working_dir) if self._working_dir else None
                    role_cfg = get_role_config(self._active_role, working_dir=_wdir)
                    set_autonomous(bool(role_cfg.get("autonomous", False)))
                    # PREV-1: Apply preview_confirmation flag from workspace config
                    _merged_cfg = load_merged_config(_wdir)
                    set_require_preview_confirmation(
                        bool(_merged_cfg.get("preview_confirmation", False))
                    )
                    # Apply max_turns to the graph state (best-effort)
                    _max_turns = role_cfg.get("max_turns", 50)
                except Exception as _rc_err:
                    _logger.debug(f"AUTO-02: role config apply failed: {_rc_err}")
                    _max_turns = 50

                # AUTO-03: map TUI role name to CodingAgent system-prompt name
                prompt_name = TUI_ROLE_TO_PROMPT.get(self._active_role, "operational")

                # Use public message-list API; fall back gracefully if shape differs
                msg_mgr = getattr(self._orchestrator, "msg_mgr", None) or getattr(
                    self._orchestrator, "message_manager", None
                )
                # Append the user message BEFORE reading the list so that
                # run_agent_once sees it as messages[-1] and sets task=text.
                if msg_mgr is not None:
                    msg_mgr.append("user", text)
                    messages = list(getattr(msg_mgr, "messages", []))
                elif callable(getattr(self._orchestrator, "get_messages", None)):
                    messages = list(self._orchestrator.get_messages())
                    messages.append({"role": "user", "content": text})
                else:
                    messages = [{"role": "user", "content": text}]
                tools = self._orchestrator.get_tools_for_role("operational")
                # MID-INJ: Attach self so inference_loop can expose
                # pop_pending_injections() to the graph via initial_state.
                self._orchestrator._injection_source = self
                result = self._orchestrator.run_agent_once(
                    system_prompt_name=prompt_name,
                    messages=messages,
                    tools=tools,
                    cancel_event=self._cancel_event,
                )
                self._orchestrator.flush_execution_trace()
                # run_agent_once() returns {"assistant_message": ..., "work_summary": ...}
                # Keep fallbacks for "response" and "last_result" for backwards compat.
                content = (
                    result.get("assistant_message")
                    or result.get("response")
                    or (result.get("last_result") or {}).get("output", "")
                )
                if content:
                    with self._history_lock:
                        self.history.append(("assistant", content))
                    self._save_history()
                    self._post(AgentFinalResponse(content=content))
                # §10.4 — capture state for /continue.  Write via
                # call_from_thread so the Textual event-loop thread owns the
                # assignment and there is no data race.
                _r = result
                self.app.call_from_thread(
                    lambda r=_r, _app=self.app: setattr(_app, "_continue_state", r)
                )
            # else: mock mode — events come through EventBus from mock_engine
        except Exception as exc:
            # Log full stack at ERROR level and preserve original behaviour
            _logger.exception(f"Agent error: {exc}")
            self._post(WorkerError(message=str(exc), traceback=""))
        finally:
            with self._agent_lock:
                self._agent_running = False
            self._post(AgentRunningEvent(running=False))
            # TASK-05: persist session snapshot after each agent run so headless /
            # autonomous runs are captured even without a UI quit event.
            try:
                self.app._save_session_snapshot()
            except Exception as _snap_err:
                _logger.debug(f"_run_agent: session snapshot failed: {_snap_err}")

    def interrupt(self) -> None:
        """Single Escape — set cancel event (§9.4)."""
        self._cancel_event.set()

    def force_interrupt(self) -> None:
        """Double-Escape — force stop (§9.5)."""
        self._cancel_event.set()
        with self._agent_lock:
            running = self._agent_running
            if running:
                self._agent_running = False
        if running:
            from tui.tui_src.ui.bus import AgentRunningEvent

            self._post(AgentRunningEvent(running=False))

    def pop_pending_injections(self) -> list[str]:
        """Return and clear all buffered mid-run messages (thread-safe)."""
        with self._agent_lock:
            msgs = list(self._pending_injections)
            self._pending_injections.clear()
        return msgs

    def get_turn_count(self) -> int:
        """Return the current turn counter from the orchestrator's graph state.

        Falls back to 0 if the orchestrator is unavailable or the state key
        is not present (mock mode).
        """
        try:
            if self._orchestrator is None:
                return 0
            # The orchestrator exposes the latest state via _last_state or
            # the graph's current state; try several attribute names.
            for attr in ("_last_state", "_current_state", "_agent_state"):
                state = getattr(self._orchestrator, attr, None)
                if isinstance(state, dict) and "turn_count" in state:
                    return int(state["turn_count"] or 0)
            # Fallback: count message pairs in our local history
            with self._history_lock:
                assistant_msgs = sum(
                    1 for role, _ in self.history if role == "assistant"
                )
            return assistant_msgs
        except Exception:
            return 0

    def get_usage_totals(self) -> tuple[int, int]:
        """Return ``(input_tokens, output_tokens)`` accumulated this session.

        Reads from the orchestrator's token-budget monitor when available;
        falls back to (0, 0) in mock mode.
        """
        try:
            if self._orchestrator is None:
                return (0, 0)
            monitor = getattr(self._orchestrator, "token_monitor", None)
            if monitor is None:
                return (0, 0)
            task_id = (
                getattr(self._orchestrator, "_current_task_id", "default") or "default"
            )
            budget = monitor.get_budget(session_id=task_id)
            return (int(budget.prompt_tokens), int(budget.completion_tokens))
        except Exception:
            return (0, 0)
