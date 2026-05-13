"""Orchestrator that manages the agent runner, tool registry, preflight checks and execution.

This module provides:
- ToolRegistry: simple in-memory mapping of tools
- Orchestrator: wires ProviderManager events, publishes telemetry, performs non-blocking model checks
- Example builtin tools and example_registry helper
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.orchestration.event_bus import EventBus, subscribe_stderr_fallback
from src.core.orchestration.work_summary import _generate_work_summary
from src.core.orchestration.tool_result_formatter import TOOL_RESULT_FORMATTERS
from src.core.orchestration.tool_constants import (
    WRITE_TOOLS_REQUIRING_READ,
    PERMISSION_REQUIRED_TOOLS,
    DRY_RUN_BLOCKED_TOOLS,
    _write_permission_audit,
)

# Backwards-compatible re-exports so external callers and tests can import
# these names directly from the orchestrator module.
__all__ = [
    "EventBus",
    "_generate_work_summary",
    "TOOL_RESULT_FORMATTERS",
    "WRITE_TOOLS_REQUIRING_READ",
    "PERMISSION_REQUIRED_TOOLS",
    "DRY_RUN_BLOCKED_TOOLS",
    "_write_permission_audit",
]

# Phase A: constants — re-exported for backward compatibility.

# tool_contracts — kept here so tests can patch orchestrator.get_tool_contract.
try:
    from src.core.orchestration.tool_contracts import get_tool_contract
    from src.core.orchestration.tool_contracts import ToolContract  # type: ignore[attr-defined]
except ImportError:

    def get_tool_contract(name: str) -> Any:  # type: ignore[misc]
        return None

    class ToolContract:  # type: ignore[no-redef]
        @staticmethod
        def model_validate(obj: Any) -> Any:
            return obj


logger = logging.getLogger(__name__)

# Provider manager sync helper expected by tests to be reachable from orchestrator

# ── TUI-03/TUI-04: approval gate registries — moved to approval_gate.py ──────
# Re-exported here for backward compatibility (file_tools.py imports
# register_bash_gate and _bash_denied from this module).

# Phase B: ToolRegistry and example_registry live in their own modules.
# Re-exported here so existing callers (tests, main.py) continue to work.
from src.core.orchestration.tool_registry import ToolRegistry  # noqa: E402
from src.core.orchestration.registry_builder import example_registry  # noqa: E402

try:
    from src.core.inference.llm_manager import (
        _ensure_provider_manager_initialized_sync,
    )
except Exception:
    # Expose a benign fallback so tests can monkeypatch the symbol on the
    # orchestrator module without importing the full llm_manager.
    def _ensure_provider_manager_initialized_sync() -> None:  # type: ignore[misc]
        return None


class Orchestrator:
    # Attributes populated by bootstrap_orchestrator() or other runtime wiring.
    # Declared here so static type-checkers (pyright) know these attributes exist
    # even though they are assigned dynamically during bootstrap.
    msg_mgr: Any
    session_mgr: Any
    _usage_buffer: Dict[str, int]
    _step_snapshot_id: Optional[str]
    _current_snapshot_id: Optional[str]
    rollback_manager: Any
    current_role: Optional[str]
    cancel_event: Any
    _pending_delegations: Any
    session_store: Any
    _tool_executor: Any
    _graph_executor: Any
    # _normalize_tool_result is a method; do not shadow it with an attribute.
    _dry_run_log: Any
    _agent_mode: Any
    _pending_preview_id: Any
    _plan_approval_event: Any
    _plan_approved: bool
    explore_mode: bool
    role_manager: Any
    _permission_gate: Any
    _permission_granted: bool
    preview_coordinator: Any
    # Plan mode controller (set during bootstrap)
    plan_mode: Any
    _plan_mode_approved: Any
    mcp_manager: Any
    _http_server_thread: Any
    lifecycle_manager: Any
    token_monitor: Any
    context_controller: Any
    cost_tracker: Any
    tool_execution_service: Any
    _mcp_server: Any

    def __init__(
        self,
        adapter: Any = None,
        tool_registry: Optional[ToolRegistry] = None,
        working_dir: Optional[str | Path] = None,
        allow_external_working_dir: bool = False,
        message_max_tokens: Optional[int] = 4000,
        deterministic: bool = False,
        seed: Optional[int] = None,
        event_bus: Optional["EventBus"] = None,
        dry_run: bool = False,
    ):
        self._dry_run = dry_run
        self._adapter = adapter
        self._provider_name = ""  # set during provider resolution
        self.deterministic = bool(deterministic)
        self.seed = seed
        self.tool_registry = tool_registry if tool_registry else example_registry()
        # TASK-09: Filter tool registry using the active permission context (set by
        # --allowed-tools / --deny-tool / --deny-prefix CLI flags in main.py).
        try:
            from src.tools.permission_context import _ACTIVE_CONTEXT as _pctx

            if _pctx is not None and not _pctx.is_empty():
                _filtered = _pctx.filter_registry(self.tool_registry)
                if isinstance(_filtered, ToolRegistry):
                    self.tool_registry = _filtered
        except Exception:
            pass
        # TUI-01: accept an externally-created EventBus (e.g. from AgentBridge)
        self.event_bus = event_bus if event_bus is not None else EventBus()
        # P2-T5: register stderr fallback for safety events so they are always
        # visible in headless/CLI mode (sandbox degradation, doom-loop detection).
        subscribe_stderr_fallback(self.event_bus)
        # Set working_dir and _allow_external now so bootstrap can use them
        self.working_dir = Path(working_dir) if working_dir else None
        self._allow_external = allow_external_working_dir  # boolified in bootstrap
        # bootstrap_orchestrator will populate many attributes (executors,
        # managers, coordinators).  We declare these at class-level so static
        # checkers know they exist; no runtime action needed here.

        # Delegate all remaining subsystem wiring to bootstrap
        from src.core.orchestration.orchestrator_bootstrap import bootstrap_orchestrator

        bootstrap_orchestrator(self, message_max_tokens)

    async def start_mcp_server(self) -> None:
        """Start the MCP STDIO server for IDE integration."""
        from src.core.orchestration.orchestrator_helpers import start_mcp_server_impl

        await start_mcp_server_impl(self)

    # ------------------------------------------------------------------
    # ARCH-1: Property shims — delegate session state to self.session_mgr.
    # ------------------------------------------------------------------

    @property
    def _session_read_files(self) -> set:  # type: ignore[override]
        """Files read during the current task (delegated to session_mgr)."""
        try:
            return self.session_mgr.read_files
        except AttributeError:
            return self.__dict__.get("_session_read_files_fallback", set())

    @_session_read_files.setter
    def _session_read_files(self, value: set) -> None:
        try:
            self.session_mgr._read_files = value  # type: ignore[attr-defined]
        except AttributeError:
            self.__dict__["_session_read_files_fallback"] = value

    @property
    def _session_modified_files(self) -> set:  # type: ignore[override]
        """Files modified during the current task (delegated to session_mgr)."""
        try:
            return self.session_mgr.modified_files
        except AttributeError:
            return self.__dict__.get("_session_modified_files_fallback", set())

    @_session_modified_files.setter
    def _session_modified_files(self, value: set) -> None:
        try:
            self.session_mgr._modified_files = value  # type: ignore[attr-defined]
        except AttributeError:
            self.__dict__["_session_modified_files_fallback"] = value

    @property
    def _current_task_id(self) -> "Optional[str]":  # type: ignore[override]
        """Current task identifier (delegated to session_mgr)."""
        try:
            return self.session_mgr.task_id
        except AttributeError:
            return self.__dict__.get("__current_task_id_init")

    @_current_task_id.setter
    def _current_task_id(self, value: "Optional[str]") -> None:
        try:
            self.session_mgr.task_id = value
        except AttributeError:
            self.__dict__["__current_task_id_init"] = value

    def _publish_active_config(self):
        from src.core.orchestration.orchestrator_helpers import (
            _publish_active_config_impl,
        )

        return _publish_active_config_impl(self)

    @property
    def adapter(self):
        return self._adapter

    @adapter.setter
    def adapter(self, value):
        self._adapter = value
        self._publish_active_config()

    def get_budget_status(self, session_id: str = "default") -> Dict[str, Any]:
        """Get current token budget status for UI display."""
        from src.core.orchestration.orchestrator_helpers import get_budget_status_impl

        return get_budget_status_impl(self, session_id)

    # HR-5: canonical dangerous-pattern list from _security.py.
    try:
        from src.tools._security import DANGEROUS_PATTERNS as _dp

        _BASH_DANGEROUS_PATTERNS: list = list(_dp)
    except Exception:  # pragma: no cover
        _BASH_DANGEROUS_PATTERNS = ["&&", "||", ";", "|", ">", "rm -rf", "git push"]

    def preflight_check(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        from src.core.orchestration.tool_preflight import preflight_check_impl

        return preflight_check_impl(self, tool_call)

    def _compact_messages(self, messages: list) -> str:
        from src.core.orchestration.orchestrator_helpers import _compact_messages_impl

        return _compact_messages_impl(self, messages)

    def begin_step_transaction(self) -> str:
        from src.core.orchestration.orchestrator_helpers import (
            begin_step_transaction_impl,
        )

        return begin_step_transaction_impl(self)

    def rollback_step_transaction(self) -> dict:
        from src.core.orchestration.orchestrator_helpers import (
            rollback_step_transaction_impl,
        )

        return rollback_step_transaction_impl(self)

    def _get_tool_timeout(self, tool_name: str) -> int:
        """Get timeout in seconds for a tool. T9: Tool Timeout Protection."""
        from src.core.orchestration.orchestrator_helpers import _get_tool_timeout_impl

        return _get_tool_timeout_impl(self, tool_name)

    def _normalize_tool_result(self, r: Any) -> Dict[str, Any]:
        """Ensure tool results conform to a minimal contract.

        Parameter name intentionally short (``r``) to match common test
        monkeypatch callables that use ``lambda r: ...``. This avoids a
        pyright false-positive about parameter-name mismatches when tests
        assign to this attribute.
        """
        from src.core.orchestration.orchestrator_helpers import (
            _normalize_tool_result_impl,
        )

        return _normalize_tool_result_impl(self, r)

    def execute_tool(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        # Phase C: delegate to module-level function in tool_execution_pipeline.py
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        return execute_tool_impl(self, tool_call)

    def _read_execution_trace(self) -> list:
        from src.core.orchestration.execution_trace import _read_execution_trace_impl

        return _read_execution_trace_impl(self)

    def _normalize_args(self, a: Any):
        from src.core.orchestration.execution_trace import _normalize_args_impl

        return _normalize_args_impl(self, a)

    def _append_execution_trace(self, entry: dict):
        from src.core.orchestration.execution_trace import _append_execution_trace_impl

        return _append_execution_trace_impl(self, entry)

    def flush_execution_trace(self):
        """Flush buffered trace entries to disk once per task (called by run_agent_once)."""
        from src.core.orchestration.execution_trace import flush_execution_trace_impl

        return flush_execution_trace_impl(self)

    def compact_context(self) -> bool:
        """S9-B: Immediately distill the current conversation history."""
        from src.core.orchestration.orchestrator_helpers import compact_context_impl

        return compact_context_impl(self)

    def _clear_execution_trace(self):
        from src.core.orchestration.execution_trace import _clear_execution_trace_impl

        return _clear_execution_trace_impl(self)

    def _publish_session_changes(self):
        """Publish session changes to event bus for sidebar display."""
        self.session_mgr.publish_files_changed()

    def _check_loop_prevention(self, tool_name: Optional[str], tool_args: dict) -> bool:
        from src.core.orchestration.execution_trace import _check_loop_prevention_impl

        return _check_loop_prevention_impl(self, tool_name, tool_args)

    def _flush_usage_buffer(self) -> None:
        """F17: Legacy flush method — delegates to SessionCostTracker."""
        from src.core.orchestration.orchestrator_helpers import _flush_usage_buffer_impl

        return _flush_usage_buffer_impl(self)

    def _create_session_snapshot(self) -> None:
        """Create a session snapshot for resume capability."""
        from src.core.orchestration.orchestrator_helpers import (
            _create_session_snapshot_impl,
        )

        return _create_session_snapshot_impl(self)

    def _ensure_working_dir(self):
        from src.core.orchestration.orchestrator_helpers import _ensure_working_dir_impl

        return _ensure_working_dir_impl(self)

    def _background_model_check(self):
        from src.core.orchestration.orchestrator_helpers import (
            _background_model_check_impl,
        )

        return _background_model_check_impl(self)

    def _publish_git_status(self) -> None:
        """TUI-07: Publish git.branch event so the TUI sidebar stays current."""
        from src.core.orchestration.orchestrator_helpers import _publish_git_status_impl

        return _publish_git_status_impl(self)

    def start_new_task(self) -> str:
        """Start a new task — delegates to task_lifecycle.start_new_task_impl."""
        from src.core.orchestration.task_lifecycle import start_new_task_impl

        return start_new_task_impl(self)

    def restore_continue_state(self, state: dict) -> None:
        """Restore continue state — delegates to task_lifecycle.restore_continue_state_impl."""
        from src.core.orchestration.task_lifecycle import restore_continue_state_impl

        return restore_continue_state_impl(self, state)

    def _sync_session_state(self) -> None:
        """Sync session state — delegates to task_lifecycle.sync_session_state_impl."""
        from src.core.orchestration.task_lifecycle import sync_session_state_impl

        return sync_session_state_impl(self)

    def get_current_task_id(self) -> Optional[str]:
        """Get the current task ID — delegates to task_lifecycle.get_current_task_id_impl."""
        from src.core.orchestration.task_lifecycle import get_current_task_id_impl

        return get_current_task_id_impl(self)

    def get_file_lock_manager(self):
        """Get the FileLockManager — delegates to task_lifecycle.get_file_lock_manager_impl."""
        from src.core.orchestration.task_lifecycle import get_file_lock_manager_impl

        return get_file_lock_manager_impl(self)

    def approve_plan(self) -> None:
        """Approve plan — delegates to task_lifecycle.approve_plan_impl."""
        from src.core.orchestration.task_lifecycle import approve_plan_impl

        return approve_plan_impl(self)

    def reject_plan(self) -> None:
        """Reject plan — delegates to task_lifecycle.reject_plan_impl."""
        from src.core.orchestration.task_lifecycle import reject_plan_impl

        return reject_plan_impl(self)

    async def wait_for_plan_approval(self) -> bool:
        """Suspend for plan approval — delegates to task_lifecycle.wait_for_plan_approval_impl."""
        from src.core.orchestration.task_lifecycle import wait_for_plan_approval_impl

        return await wait_for_plan_approval_impl(self)

    def get_tools_for_role(self, role: str) -> List[Dict[str, Any]]:
        """Return role-filtered tools — delegates to task_lifecycle.get_tools_for_role_impl."""
        from src.core.orchestration.task_lifecycle import get_tools_for_role_impl

        return get_tools_for_role_impl(self, role)

    def run_agent_once(
        self,
        system_prompt_name,
        messages,
        tools,
        cancel_event=None,
    ):
        """Invokes the LangGraph cognitive pipeline — delegates to inference_loop.run_agent_once_impl."""
        from src.core.orchestration.inference_loop import run_agent_once_impl

        return run_agent_once_impl(
            self, system_prompt_name, messages, tools, cancel_event
        )

    # ------------------------------------------------------------------
    # S6-B: Provider family → prompt partial selection
    # ------------------------------------------------------------------

    # Re-export the map from provider_capabilities for backward compat
    from src.core.orchestration.provider_capabilities import (  # noqa: E402
        _PROVIDER_FAMILY_MAP,
    )

    @classmethod
    def _map_provider_family(cls, raw_name: str) -> str:
        """Map a provider name to a canonical family string."""
        from src.core.orchestration.provider_capabilities import (
            _map_provider_family_impl,
        )

        return _map_provider_family_impl(raw_name)

    def get_provider_capabilities(self) -> Dict[str, Any]:
        """Get provider capabilities — delegates to provider_capabilities module."""
        from src.core.orchestration.provider_capabilities import (
            get_provider_capabilities_impl,
        )

        return get_provider_capabilities_impl(self)

    def close(self) -> None:
        """Release long-lived resources (executor thread pools, MCP subprocesses)."""
        try:
            if hasattr(self, "_graph_executor"):
                self._graph_executor.shutdown(wait=True)
        except Exception:
            pass
        try:
            if hasattr(self, "_tool_executor"):
                self._tool_executor.shutdown(wait=True)
        except Exception:
            pass
        # SEC-VOL23-1: join the session-title background thread with a short
        # timeout so a partially-written title is not abandoned on process exit.
        try:
            _title_thread = getattr(self, "_session_title_thread", None)
            if _title_thread is not None and _title_thread.is_alive():
                _title_thread.join(timeout=2.0)
        except Exception:
            pass
        # P1: kill MCP subprocesses so stdio child processes are not orphaned.
        try:
            _mcp = getattr(self, "mcp_manager", None)
            if _mcp is not None and hasattr(_mcp, "stop_sync"):
                _mcp.stop_sync()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
