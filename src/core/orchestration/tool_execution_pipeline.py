"""tool_execution_pipeline.py — Top-level orchestrator for tool execution.

TASK-22: Stage implementations now live under
``src/core/orchestration/pipeline/`` (``preflight.py``, ``permissions.py``,
``dispatch.py``, ``post_hooks.py``).  This module coordinates
``execute_tool_impl(orch, tool_call)`` and re-exports stage symbols for
backwards compatibility.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from src.core.messaging.event_types import ToolExecuteStart
from src.core.orchestration.pipeline.dispatch import (
    TOOL_EXECUTOR_MAX_WORKERS,
    _CURRENT_TOOL_CANCEL_EVENT,
    _CURRENT_TOOL_PROCESS,
    _THREAD_CANCEL_EVENTS,
    _THREAD_CANCEL_LOCK,
    _THREAD_PROCESS_LOCK,
    _THREAD_TOOL_PROCESSES,
    _TOOL_EXECUTOR_LOCK,
    _dispatch_tool_call,
    _handle_tool_execution_error,
    _kill_proc_group,
    _timeout_result,
    is_current_tool_cancelled,
    register_current_tool_process,
    unregister_current_tool_process,
)
from src.core.orchestration.pipeline.permissions import (
    APPROVAL_TIMEOUT_SECONDS,
    _ACTIVE_AGENT_CTX,
    _check_agent_permission_override,
    _check_explore_mode_guard,
    _check_permission_mode_guard,
    _check_plan_mode_guard,
    _check_workdir_confinement,
    _run_permission_gate,
    discard_tool_denied,
    is_tool_denied,
    register_tool_gate,
)
from src.core.orchestration.pipeline.post_hooks import (
    RESULT_MAX_CHARS,
    TIER_TOOL_OUTPUT_MAX_CHARS,
    TOOL_OUTPUT_MAX_CHARS,
    ToolContract,
    ValidationError,
    _format_tool_result,
    _run_post_execution,
    _truncate_result_strings,
    get_tool_contract,
    resolve_tool_output_max_chars,
)
from src.core.orchestration.pipeline.preflight import (
    _check_read_before_write,
    _check_workspace_scope_guard,
    _extract_path_arg,
    _run_preflight_and_lookup,
    _run_sandbox_and_snapshot,
)
from src.core.orchestration.tool_constants import (
    DRY_RUN_BLOCKED_TOOLS,
    PERMISSION_REQUIRED_TOOLS,
    WRITE_TOOLS_REQUIRING_READ,
    _write_permission_audit,
)
from src.core.security.redaction import redact_data, redact_secrets

logger = logging.getLogger(__name__)

try:
    from src.core.logger import logger as guilogger  # type: ignore[assignment]
except Exception:
    guilogger = logger  # type: ignore[assignment]


def execute_tool_impl(orch: Any, tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Module-level implementation of ``Orchestrator.execute_tool``.

    Pipeline stages:
      1. Input validation + alias normalization + dry-run short-circuit
      2. Guard checks (read-before-write, scope, plan-mode, explore, permission-mode)
      3. Preflight + registry lookup + agent/role tool checks   (``pipeline.preflight``)
      4. Workdir confinement + approval permission gate         (``pipeline.permissions``)
      5. Input contract validation + syntax sandbox + snapshot  (``pipeline.preflight``)
      6. Pre-tool hook + threaded timeout dispatch              (``pipeline.dispatch``)
      7. Normalization, redaction, post-hooks & telemetry       (``pipeline.post_hooks``)
    """
    name_raw = tool_call.get("name")
    if not isinstance(name_raw, str):
        return {"ok": False, "error": "Tool name must be a string."}

    name = name_raw
    args = tool_call.get("arguments") or {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "Tool arguments must be a mapping."}

    # 0. Alias normalisation BEFORE any permission/policy check (SEC-2 / HS-4).
    try:
        from src.tools.tools_config import resolve_tool_alias

        name = resolve_tool_alias(name)
    except Exception:
        pass

    if getattr(orch, "_dry_run", False) and name in DRY_RUN_BLOCKED_TOOLS:
        args = dict(args)
        args.pop("user_approved", None)
        entry = {"tool": name, "args": redact_data(args)}
        try:
            orch._dry_run_log.append(entry)
        except Exception:
            pass
        return {"status": "dry_run", "would_call": name, "args": redact_data(args)}

    tool_call_id = uuid.uuid4().hex[:8]

    for _guard in (
        _check_read_before_write(orch, name, args, WRITE_TOOLS_REQUIRING_READ),
        _check_workspace_scope_guard(orch, name, args, WRITE_TOOLS_REQUIRING_READ),
        _check_plan_mode_guard(orch, name),
        _check_explore_mode_guard(orch, name),
        _check_permission_mode_guard(orch, name),
    ):
        if _guard is not None:
            return _guard

    preflight_err, tool = _run_preflight_and_lookup(
        orch, name, args, tool_call, tool_call_id
    )
    if preflight_err is not None:
        return preflight_err

    resolved_name = (
        tool_call.pop("_resolved_name", None) if isinstance(tool_call, dict) else None
    )
    if isinstance(resolved_name, str) and resolved_name and resolved_name != name:
        name = resolved_name
        for _guard in (
            _check_read_before_write(orch, name, args, WRITE_TOOLS_REQUIRING_READ),
            _check_workspace_scope_guard(orch, name, args, WRITE_TOOLS_REQUIRING_READ),
            _check_plan_mode_guard(orch, name),
            _check_explore_mode_guard(orch, name),
            _check_permission_mode_guard(orch, name),
        ):
            if _guard is not None:
                return _guard

    try:
        _write_permission_audit(
            orch.working_dir, name, args, "allow", "passed_all_gates"
        )
    except Exception:
        pass

    needs_gate = _check_workdir_confinement(
        orch,
        name,
        args,
        permission_required_tools=PERMISSION_REQUIRED_TOOLS,
    )
    permission_gate = _run_permission_gate(orch, name, args, tool_call_id, needs_gate)
    if permission_gate is not None:
        return permission_gate

    try:
        orch.event_bus.publish_typed(
            ToolExecuteStart(
                tool=name,
                args={k: str(v)[:200] for k, v in args.items() if k != "content"},
                tool_call_id=tool_call_id,
            )
        )
    except Exception:
        pass

    try:
        contract = get_tool_contract(name)
        if contract is not None:
            input_contract: Any = (
                getattr(contract, "input_model", contract)
                if isinstance(contract, type)
                else contract
            )
            try:
                input_contract.model_validate(args)
            except ValidationError as ve:
                return {
                    "ok": False,
                    "error": f"Tool call failed contract validation: {ve}",
                }
    except Exception as exc:
        guilogger.error(
            "tool_execution_pipeline: contract validation failed for tool %r (fail-closed): %s",
            name,
            exc,
        )
        return {
            "ok": False,
            "error": f"Tool call failed contract validation: {exc}",
        }

    # Strip LLM-injected user_approved (prevents WorkspaceGuard bypass)
    args.pop("user_approved", None)

    path_arg = _extract_path_arg(args)
    sandbox_err = _run_sandbox_and_snapshot(orch, name, args, tool, path_arg)
    if sandbox_err is not None:
        return sandbox_err

    if name == "batch":
        try:
            from src.tools.batch_tools import set_batch_orchestrator

            set_batch_orchestrator(orch)
        except Exception:
            pass

    try:
        hook_runner = getattr(orch, "_tool_hook_runner", None)
        if hook_runner is None:
            from src.core.orchestration.deferred_init import is_trusted
            from src.core.orchestration.tool_hooks import ToolHookRunner

            hook_runner = ToolHookRunner(
                working_dir=orch.working_dir,
                trusted=is_trusted(orch),
            )
            orch._tool_hook_runner = hook_runner
        hook_result = hook_runner.run_pre(name, args)
        if not hook_result.allowed:
            return {
                "ok": False,
                "error": f"Pre-tool hook denied '{name}': {hook_result.reason}",
            }
    except Exception:
        pass

    timeout_seconds = orch._get_tool_timeout(name)

    try:
        res = _dispatch_tool_call(orch, name, args, tool, tool_call_id, timeout_seconds)
    except TimeoutError:
        guilogger.warning(f"Tool '{name}' timed out after {timeout_seconds}s")
        return _timeout_result(name, timeout_seconds)
    except Exception as exc:
        return _handle_tool_execution_error(orch, name, args, tool_call_id, exc)

    return _run_post_execution(
        orch,
        name,
        args,
        tool,
        tool_call_id,
        res,
        path_arg,
        get_tool_contract_fn=get_tool_contract,
    )


__all__ = [
    "APPROVAL_TIMEOUT_SECONDS",
    "DRY_RUN_BLOCKED_TOOLS",
    "PERMISSION_REQUIRED_TOOLS",
    "RESULT_MAX_CHARS",
    "TIER_TOOL_OUTPUT_MAX_CHARS",
    "TOOL_EXECUTOR_MAX_WORKERS",
    "TOOL_OUTPUT_MAX_CHARS",
    "ToolContract",
    "ValidationError",
    "WRITE_TOOLS_REQUIRING_READ",
    "_ACTIVE_AGENT_CTX",
    "_CURRENT_TOOL_CANCEL_EVENT",
    "_CURRENT_TOOL_PROCESS",
    "_THREAD_CANCEL_EVENTS",
    "_THREAD_CANCEL_LOCK",
    "_THREAD_PROCESS_LOCK",
    "_THREAD_TOOL_PROCESSES",
    "_TOOL_EXECUTOR_LOCK",
    "_check_agent_permission_override",
    "_check_explore_mode_guard",
    "_check_permission_mode_guard",
    "_check_plan_mode_guard",
    "_check_read_before_write",
    "_check_workdir_confinement",
    "_check_workspace_scope_guard",
    "_dispatch_tool_call",
    "_format_tool_result",
    "_handle_tool_execution_error",
    "_kill_proc_group",
    "_run_permission_gate",
    "_run_post_execution",
    "_run_preflight_and_lookup",
    "_run_sandbox_and_snapshot",
    "_timeout_result",
    "_truncate_result_strings",
    "discard_tool_denied",
    "execute_tool_impl",
    "get_tool_contract",
    "is_current_tool_cancelled",
    "is_tool_denied",
    "redact_data",
    "redact_secrets",
    "register_current_tool_process",
    "register_tool_gate",
    "resolve_tool_output_max_chars",
    "unregister_current_tool_process",
]
