"""execution_helpers.py — Backward-compatibility shim (P3-4 refactor).

All symbols are re-exported from the focused sub-modules below.
Callers that import from this file continue to work unchanged.

Sub-modules (import directly for cleaner dependency graphs):
  - execution_parsing   : extract_tool_call_from_response, build_no_action_result
  - execution_lifecycle : retry, resolve, cancellation, logging helpers
  - execution_dispatch  : generate_action_for_plan_step
  - execution_preflight : handle_execution_preflight_and_role_gate
  - execution_tool      : emit/sync/preview/dispatch/tracking helpers
  - execution_plan      : compute_* and build_* payload functions
  - execution_guards    : _validate_python_syntax, _capture_snapshot, check_agent_definition_tool_gate
"""
from __future__ import annotations

# Guards (previously extracted)
from src.core.orchestration.graph.nodes.execution_guards import (  # noqa: F401
    _validate_python_syntax,
    _capture_snapshot,
    check_agent_definition_tool_gate,
)

# Parsing
from src.core.orchestration.graph.nodes.execution_parsing import (  # noqa: F401
    extract_tool_call_from_response,
    build_no_action_result,
)

# Lifecycle
from src.core.orchestration.graph.nodes.execution_lifecycle import (  # noqa: F401
    increment_step_retry_count,
    resolve_execution_orchestrator,
    maybe_build_execution_cancellation_result,
    select_execution_action,
    maybe_begin_step_transaction,
    log_wave_execution_start,
    log_plan_and_wave_advancement,
    log_plan_step_execution,
    sync_tool_result_to_ui,
    log_no_action_outcome,
    sync_execution_state_to_orchestrator,
)

# Dispatch (LLM-driven step generation)
from src.core.orchestration.graph.nodes.execution_dispatch import (  # noqa: F401
    generate_action_for_plan_step,
)

# Preflight / gate checks
from src.core.orchestration.graph.nodes.execution_preflight import (  # noqa: F401
    handle_execution_preflight_and_role_gate,
)

# Tool execution helpers
from src.core.orchestration.graph.nodes.execution_tool import (  # noqa: F401
    emit_plan_progress_and_sync_todo,
    emit_execution_step_start,
    emit_execution_step_finish,
    maybe_build_preview_result,
    handle_read_then_write_success,
    schedule_async_post_tool_hook,
    dispatch_execution_tool,
    update_tool_tracking,
)

# Plan computation & payload builders
from src.core.orchestration.graph.nodes.execution_plan import (  # noqa: F401
    compute_execution_post_tool_updates,
    compute_plan_step_updates,
    compute_no_plan_fail_update,
    compute_plan_approval_consumed,
    compute_affected_files_update,
    build_read_then_write_result,
    build_tool_history_messages,
    compute_replan_trigger,
    compute_plan_progress_payload,
    compute_plan_exit_update,
    build_execution_return_payload,
)
