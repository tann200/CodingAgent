"""Decomposed tool execution pipeline stages (TASK-22).

Stages:
1. ``preflight``   — read-before-write, workspace scope, registry lookup, syntax sandbox, snapshots
2. ``permissions`` — plan/explore/permission-mode guards, workdir confinement, policy & approval gate
3. ``dispatch``    — workdir injection, worker thread pool, timeout, cancellation & error handling
4. ``post_hooks``  — truncation, redaction, post-hooks, contract validation, file tracking & telemetry
"""

from src.core.orchestration.pipeline.dispatch import (
    TOOL_EXECUTOR_MAX_WORKERS,
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
)
from src.core.orchestration.pipeline.post_hooks import (
    RESULT_MAX_CHARS,
    TIER_TOOL_OUTPUT_MAX_CHARS,
    TOOL_OUTPUT_MAX_CHARS,
    _run_post_execution,
    _truncate_result_strings,
    resolve_tool_output_max_chars,
)
from src.core.orchestration.pipeline.preflight import (
    _check_read_before_write,
    _check_workspace_scope_guard,
    _extract_path_arg,
    _run_preflight_and_lookup,
    _run_sandbox_and_snapshot,
)

__all__ = [
    "APPROVAL_TIMEOUT_SECONDS",
    "RESULT_MAX_CHARS",
    "TIER_TOOL_OUTPUT_MAX_CHARS",
    "TOOL_EXECUTOR_MAX_WORKERS",
    "TOOL_OUTPUT_MAX_CHARS",
    "_ACTIVE_AGENT_CTX",
    "_check_agent_permission_override",
    "_check_explore_mode_guard",
    "_check_permission_mode_guard",
    "_check_plan_mode_guard",
    "_check_read_before_write",
    "_check_workdir_confinement",
    "_check_workspace_scope_guard",
    "_dispatch_tool_call",
    "_extract_path_arg",
    "_handle_tool_execution_error",
    "_kill_proc_group",
    "_run_permission_gate",
    "_run_post_execution",
    "_run_preflight_and_lookup",
    "_run_sandbox_and_snapshot",
    "_timeout_result",
    "_truncate_result_strings",
    "is_current_tool_cancelled",
    "register_current_tool_process",
    "resolve_tool_output_max_chars",
    "unregister_current_tool_process",
]
