"""Tool execution pipeline — Stage 4: Truncation, redaction, post-hooks & telemetry."""

from __future__ import annotations

import contextlib
import datetime
import importlib
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.core.messaging.event_types import (
    AgentModeChanged, AgentPlanCommitted, FileDeleted, FileModified,
    PreviewPending, TokenBudget, TokenBudgetUpdate, ToolExecuteFinish, ToolInvoked,
)
from src.core.security.redaction import redact_data, redact_secrets

logger = logging.getLogger("src.core.orchestration.tool_execution_pipeline")


def _pipe_logger() -> logging.Logger:
    mod = sys.modules.get("src.core.orchestration.tool_execution_pipeline")
    return getattr(mod, "logger", logger)

try:
    from src.core.logger import logger as guilogger  # type: ignore[assignment]
except Exception:
    guilogger = logger  # type: ignore[assignment]

try:
    _pyd = importlib.import_module("pydantic")
    ValidationError = getattr(_pyd, "ValidationError")
except Exception:

    class ValidationError(Exception):  # type: ignore[no-redef]
        pass


try:
    from src.core.orchestration.tool_contracts import (  # type: ignore[attr-defined]
        ToolContract,  # type: ignore[assignment]
        get_tool_contract,
    )
except ImportError:

    def get_tool_contract(name: str) -> Any:  # type: ignore[misc]
        return None

    class ToolContract:  # type: ignore[no-redef]
        @staticmethod
        def model_validate(obj: Any) -> Any:  # type: ignore[misc]
            return obj


try:
    from src.core.orchestration.tool_result_formatter import (
        format_tool_result as _format_tool_result,
    )
except Exception:

    def _format_tool_result(result: Any, tool_name: Optional[str] = None) -> str:  # type: ignore[misc]
        return str(result)


RESULT_MAX_CHARS: int = 8_000
TOOL_OUTPUT_MAX_CHARS: int = RESULT_MAX_CHARS

TIER_TOOL_OUTPUT_MAX_CHARS: Dict[str, int] = {
    "nano": 6_000, "compact": 12_000, "standard": 24_000, "frontier": 48_000,
}


def resolve_tool_output_max_chars(orch: Any = None) -> int:
    """Resolve maximum allowed characters for a tool output string based on runtime policy."""
    if orch is None:
        return RESULT_MAX_CHARS
    explicit = getattr(orch, "_tool_output_max_chars", None)
    if isinstance(explicit, int) and explicit > 0:
        return explicit

    pol = getattr(orch, "_runtime_policy", None) or getattr(orch, "runtime_policy", None)
    if pol is not None:
        val = pol.get("tool_output_max_chars") if isinstance(pol, dict) else getattr(pol, "tool_output_max_chars", None)
        if isinstance(val, int) and val > 0:
            return val
        if isinstance(pol, dict):
            tier = pol.get("policy_tier")
            if tier and str(tier) in TIER_TOOL_OUTPUT_MAX_CHARS:
                return TIER_TOOL_OUTPUT_MAX_CHARS[str(tier)]

    try:
        from src.core.orchestration.graph.runtime_policy import resolve_runtime_policy

        resolved = resolve_runtime_policy(orch)
        val = getattr(resolved, "tool_output_max_chars", None)
        if isinstance(val, int) and val > 0:
            return val
        tier = getattr(resolved, "policy_tier", None)
        if tier and str(tier) in TIER_TOOL_OUTPUT_MAX_CHARS:
            return TIER_TOOL_OUTPUT_MAX_CHARS[str(tier)]
    except Exception:
        pass
    return RESULT_MAX_CHARS


def _truncate_result_strings(
    result: Any, _depth: int = 0, max_chars: Optional[int] = None,
) -> Any:
    """Recursively truncate overly large string fields in tool results (max depth 6)."""
    ceiling = max_chars if (isinstance(max_chars, int) and max_chars > 0) else TOOL_OUTPUT_MAX_CHARS
    if _depth > 6:
        return result
    if isinstance(result, str):
        if len(result) > ceiling:
            return result[:ceiling] + f"\n... [truncated: {len(result) - ceiling} chars omitted]"
        return result
    if isinstance(result, dict):
        return {k: _truncate_result_strings(v, _depth + 1, max_chars=ceiling) for k, v in result.items()}
    if isinstance(result, list):
        return [_truncate_result_strings(v, _depth + 1, max_chars=ceiling) for v in result]
    return result


def _record_file_side_effects(
    orch: Any,
    name: str,
    args: dict,
    tool: Any,
    path_arg: Optional[str],
    lock_ctx: Any,
) -> None:
    """Update read/modified file tracking and publish file events after a successful call."""
    wd = Path(orch.working_dir or ".")
    if name in ("read_file", "fs.read", "read_file_chunk", "read_file_bytes", "files_read") and path_arg:
        try:
            resolved_path = str((wd / path_arg).resolve())
            with lock_ctx:
                orch._session_read_files.add(resolved_path)
        except Exception:
            pass
    elif name == "batched_file_read":
        try:
            batch_paths = args.get("paths") or []
            if isinstance(batch_paths, list):
                resolved_batch = [
                    str((wd / p).resolve())
                    for p in batch_paths
                    if isinstance(p, str) and p.strip()
                ]
                with lock_ctx:
                    orch._session_read_files.update(resolved_batch)
        except Exception:
            pass
    elif name == "rename_file":
        try:
            src_raw = args.get("path") or args.get("src_path") or args.get("src") or args.get("old_path")
            dst_raw = args.get("new_path") or args.get("dst_path") or args.get("dst") or args.get("new_name")
            src_res = str((wd / src_raw).resolve()) if src_raw else None
            dst_res = str((wd / dst_raw).resolve()) if dst_raw else None
            with lock_ctx:
                for p_res in (src_res, dst_res):
                    if p_res:
                        orch._session_modified_files.add(p_res)
                        orch._session_read_files.add(p_res)
                if dst_res:
                    try:
                        from src.tools.guardrails import mark_file_read

                        mark_file_read(dst_res)
                    except Exception:
                        pass
                aff = getattr(orch, "_affected_files", None)
                if isinstance(aff, list):
                    if src_res and src_res not in aff and src_raw not in aff:
                        aff.append(src_res)
                    if dst_res and dst_res not in aff and dst_raw not in aff:
                        aff.append(dst_res)
                elif isinstance(aff, set):
                    for p_res in (src_res, dst_res):
                        if p_res:
                            aff.add(p_res)
            try:
                for p_res in (src_res, dst_res):
                    if p_res:
                        orch.event_bus.publish_typed(
                            FileModified(path=p_res, tool=name, workdir=str(orch.working_dir))
                        )
            except Exception:
                pass
        except Exception:
            pass
    elif "write" in tool.get("side_effects", []) and path_arg:
        try:
            resolved_path = str((wd / path_arg).resolve())
            with lock_ctx:
                orch._session_modified_files.add(resolved_path)
            try:
                orch.event_bus.publish_typed(
                    FileModified(path=resolved_path, tool=name, workdir=str(orch.working_dir))
                )
            except Exception:
                pass
        except Exception:
            pass
    elif name == "delete_file" and path_arg:
        try:
            resolved_path = str((wd / path_arg).resolve())
            try:
                orch.event_bus.publish_typed(
                    FileDeleted(path=resolved_path, workdir=str(orch.working_dir))
                )
            except Exception:
                pass
        except Exception:
            pass


def _run_post_execution(
    orch: Any,
    name: str,
    args: dict,
    tool: Any,
    tool_call_id: str,
    res: Any,
    path_arg: Optional[str],
    *,
    get_tool_contract_fn: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Normalise result, apply side-effects, fire events, and persist to session store."""
    contract_lookup = get_tool_contract_fn or get_tool_contract

    res = orch._normalize_tool_result(res)
    max_chars = resolve_tool_output_max_chars(orch)
    res = _truncate_result_strings(res, max_chars=max_chars)
    res = redact_data(res)

    if name in ("plan_enter", "plan_exit") and isinstance(res, dict) and res.get("ok"):
        new_mode = res.get("agent_mode", "execution")
        setattr(orch, "_agent_mode", new_mode)
        try:
            orch.event_bus.publish_typed(AgentModeChanged(mode=new_mode, tool=name))
        except Exception:
            pass
        if name == "plan_exit" and res.get("steps"):
            setattr(orch, "_committed_plan_steps", res["steps"])
            setattr(orch, "_plan_mode_approved", True)
            try:
                orch.event_bus.publish_typed(
                    AgentPlanCommitted(step_count=len(res["steps"]), tool=name)
                )
            except Exception:
                pass

    try:
        hook_runner = getattr(orch, "_tool_hook_runner", None)
        if hook_runner is not None:
            hook_runner.run_post(name, args, res)
    except Exception as _hook_exc:
        _pipe_logger().warning(
            "tool_execution_pipeline: post-tool hook for %r failed: %s",
            name,
            _hook_exc,
            exc_info=True,
        )

    try:
        schema = contract_lookup(name)
        if schema and isinstance(res, dict):
            output_schema: Any = getattr(schema, "output_model", schema) if isinstance(schema, type) else schema
            try:
                output_schema.model_validate(res)
            except ValidationError:
                try:
                    output_schema.model_validate(res.get("result") or {})
                except ValidationError as ve:
                    return {"ok": False, "error": f"Tool result failed contract validation: {ve}"}
        else:
            try:
                ToolContract.model_validate({"tool": name, "args": args, "result": res})
            except ValidationError as ve:
                return {"ok": False, "error": f"Tool result failed contract validation: {ve}"}
    except Exception as exc:
        _pipe_logger().warning(
            "tool_execution_pipeline: post-exec contract validation skipped for %r (framework error: %s)",
            name, exc, exc_info=True,
        )

    _raw_lock = getattr(orch, "_state_lock", None)
    _lock_ctx = (
        _raw_lock
        if _raw_lock is not None and hasattr(_raw_lock, "__enter__")
        else contextlib.nullcontext()
    )

    try:
        ts = time.time()
        with _lock_ctx:
            orch._usage_buffer[name] = orch._usage_buffer.get(name, 0) + 1
            orch.cost_tracker.record_tool_call(name)
        try:
            orch.event_bus.publish_typed(
                ToolInvoked(
                    session_update="tool_call_update", tool_call_id=tool_call_id,
                    title=name, status="invoked", timestamp=ts, workdir=str(orch.working_dir),
                )
            )
        except Exception:
            pass
    except Exception:
        pass

    if res.get("status") == "ok" or res.get("ok") is True:
        _record_file_side_effects(orch, name, args, tool, path_arg, _lock_ctx)

    try:
        from src.core.orchestration.change_log import record_change

        record_change(orch, name, args, orch.working_dir)
    except Exception as exc:
        guilogger.debug(f"change_log: record_change skipped (non-blocking): {exc}")

    try:
        with _lock_ctx:
            orch._append_execution_trace({
                "tool": name,
                "args": redact_data(orch._normalize_args(args)),
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "result_ok": bool(res.get("status") == "ok" or res.get("ok") is True),
            })
    except Exception:
        pass

    try:
        formatted = _format_tool_result(res, name)
        orch.event_bus.publish_typed(
            ToolExecuteFinish(
                session_update="tool_call_update", tool_call_id=tool_call_id,
                title=name, status="completed",
                content=[{"type": "text", "text": redact_secrets(formatted)}],
                raw_output=redact_data(res), workdir=str(orch.working_dir),
            )
        )
    except Exception:
        pass

    try:
        if hasattr(orch, "token_monitor"):
            sid = getattr(orch, "_current_task_id", "default")
            budget = orch.token_monitor.get_budget(session_id=sid)
            orch.event_bus.publish_typed(
                TokenBudgetUpdate(
                    used_tokens=budget.used_tokens, max_tokens=budget.max_tokens,
                    usage_ratio=budget.usage_ratio, session_id=sid,
                )
            )
            used, limit = budget.used_tokens, (budget.max_tokens or 32_768)
            pct = min(100, int(used / limit * 100)) if limit else 0
            orch.event_bus.publish_typed(
                TokenBudget(used=used, limit=limit, percent=pct, warning=pct >= 80)
            )
    except Exception:
        pass

    try:
        if hasattr(orch, "preview_service") and getattr(orch, "_pending_preview_id", None):
            orch.event_bus.publish_typed(PreviewPending(preview_id=orch._pending_preview_id))
    except Exception:
        pass

    try:
        safe_args = {k: str(v) if isinstance(v, Path) else v for k, v in args.items()}
        orch.session_store.add_tool_call(
            session_id=getattr(orch, "_current_task_id", None),
            tool_name=name, args=redact_data(safe_args), result=redact_data(res), success=True,
        )
    except Exception:
        pass

    try:
        orch._sync_session_state()
    except Exception:
        pass

    return {"ok": True, "result": res}
