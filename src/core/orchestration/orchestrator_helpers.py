"""Miscellaneous orchestrator helper methods.

Extracted from orchestrator.py (Phase G5/G6) — groups small helpers that
don't belong to any single thematic module.

All functions take ``orch`` as the first argument (the Orchestrator instance).
"""

from __future__ import annotations


from src.core.messaging.event_types import ContextCompactFailed, GitBranch, McpServerStatus, ModelRouting, ProviderModelsCached, WorkingDirUnavailable
import datetime
import json
import logging
import re
import subprocess
import traceback
import time
import tempfile
import os
import shutil
from typing import Any, Dict

from src.core.logger import logger as guilogger
from src.core.utils.strings import valid_str as _valid_str, extract_str as _extract_str

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider / model config publishing
# ---------------------------------------------------------------------------


def _publish_active_config_impl(orch: Any) -> None:
    """Publish the currently active adapter / model selection to the event bus."""
    provider = ""
    model = ""
    available_models: list = []

    # 1) Prefer orchestrator-level capabilities when provided (authoritative)
    try:
        if hasattr(orch, "get_provider_capabilities") and callable(
            getattr(orch, "get_provider_capabilities")
        ):
            try:
                caps = orch.get_provider_capabilities()
                if isinstance(caps, dict):
                    provider = (
                        caps.get("provider_name")
                        or caps.get("provider")
                        or caps.get("name")
                        or ""
                    )
                    model = caps.get("model") or caps.get("default_model") or ""
            except Exception:
                pass
    except Exception:
        pass

    # 2) Fall back to central ProviderManager/provider_capabilities implementation
    if not provider or not model:
        try:
            from src.core.orchestration.provider_capabilities import (
                get_provider_capabilities_impl as _get_caps,
            )

            try:
                caps2 = _get_caps(orch)
                if isinstance(caps2, dict):
                    provider = provider or (caps2.get("provider_name") or "")
                    model = model or (caps2.get("model") or "")
            except Exception:
                pass
            # If provider/model from the central implementation are not
            # concrete (eg. MagicMock placeholders), fall back to a
            # conservative inspection of the active adapter attributes.
            try:
                from src.core.orchestration.provider_capabilities import (
                    _valid_str as _pc_valid,
                    _extract_str as _pc_extract,
                )
            except Exception:
                # Fall back to the module-level canonical helpers already imported.
                _pc_valid = _valid_str
                _pc_extract = _extract_str

            # Only inspect adapter attributes when provider/model are not
            # already resolved to concrete values.
            if (not provider or not provider.strip() or "MagicMock" in provider) or (
                not model or not model.strip() or "MagicMock" in model
            ):
                try:
                    adapter = getattr(orch, "_adapter", None)
                    if adapter is not None:
                        # Provider fallback
                        if not (
                            _pc_valid(provider) if isinstance(provider, str) else False
                        ):
                            p_attr = getattr(adapter, "provider", None)
                            p_cand = _pc_extract(p_attr)
                            if p_cand and _pc_valid(p_cand):
                                provider = p_cand
                            else:
                                # adapter may expose a simple name attribute
                                name_attr = getattr(adapter, "name", None)
                                name_cand = _pc_extract(name_attr)
                                if name_cand and _pc_valid(name_cand):
                                    provider = name_cand

                        # Model fallback
                        if not (_pc_valid(model) if isinstance(model, str) else False):
                            dm = _pc_extract(getattr(adapter, "default_model", None))
                            if dm and _pc_valid(dm):
                                model = dm
                            else:
                                ms = getattr(adapter, "models", None)
                                if isinstance(ms, list):
                                    for m in ms:
                                        m_c = _pc_extract(m)
                                        if m_c and _pc_valid(m_c):
                                            model = m_c
                                            break
                except Exception:
                    pass
        except Exception:
            # 3) ProviderManager unavailable; try direct ProviderManager lookup
            try:
                if getattr(orch, "_adapter", None):
                    from src.core.inference.llm_manager import get_provider_manager as _get_pm_local
                    pm = _get_pm_local()
                    if pm:
                        try:
                            caps = pm.get_provider_capabilities(orch._adapter)
                            if isinstance(caps, dict):
                                provider = provider or (
                                    caps.get("provider_name")
                                    or caps.get("provider")
                                    or ""
                                )
                                model = model or (
                                    caps.get("model") or caps.get("default_model") or ""
                                )
                        except Exception:
                            pass
            except Exception:
                pass

    # Publish sanitized values to the event bus. Filter available models to only
    # include concrete strings.
    if hasattr(orch, "event_bus"):
        adapter = getattr(orch, "_adapter", None)
        if adapter is not None:
            try:
                ms = getattr(adapter, "models", None)
                if isinstance(ms, list):
                    # filter in place using the existing validator
                    available_models = [str(m).strip() for m in ms if _valid_str(m)]
                else:
                    try:
                        from src.core.utils.strings import valid_str as _vs3

                        if _vs3(ms):
                            available_models = [str(ms).strip()]
                    except Exception:
                        if isinstance(ms, str) and ms.strip():
                            available_models = [str(ms).strip()]
            except Exception:
                available_models = getattr(adapter, "models", []) or []

        try:
            orch.event_bus.publish_typed(ModelRouting(selected=model or "", provider=provider or "", available_models=available_models))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------


def _compact_messages_impl(orch: Any, messages: list) -> str:
    """Callback passed to MessageManager for inline context compaction.

    When the conversation history overflows the token budget,
    MessageManager calls this method with the messages that would be
    dropped.  We generate a prose summary that is injected back into
    the conversation so the agent always has access to prior context.
    """
    try:
        from src.core.memory.distiller import compact_messages_to_prose

        return compact_messages_to_prose(messages, working_dir=orch.working_dir)
    except Exception as e:
        guilogger.warning(f"_compact_messages failed (non-fatal): {e}")
        return ""


def compact_context_impl(orch: Any) -> bool:
    """S9-B: Immediately distill the current conversation history.

    Triggers compaction regardless of the token threshold so the user can
    manually free context window space at any time via the ``/compact`` TUI
    slash command.

    Uses :class:`~src.core.memory.compaction_service.CompactionService` as the
    unified facade, which tries the LLM summariser first and falls back to the
    deterministic sliding-window compactor.

    Returns:
        True if compaction ran successfully; False on error or no history.
    """
    try:
        from pathlib import Path

        from src.core.memory.compaction_service import CompactionService

        history = list(orch.msg_mgr.messages) if hasattr(orch, "msg_mgr") else []
        if not history:
            return False

        working_dir: Path | None = None
        try:
            working_dir = Path(orch.working_dir) if hasattr(orch, "working_dir") else None
        except Exception:
            pass

        event_bus = getattr(orch, "event_bus", None)
        service = CompactionService(
            history=history,
            working_dir=working_dir,
            event_bus=event_bus,
        )
        result = service.compact()
        if result.success:
            guilogger.info(
                "compact_context: compaction complete via method=%s "
                "tokens=%d→%d",
                result.method,
                result.tokens_before,
                result.tokens_after,
            )
        else:
            guilogger.warning("compact_context: compaction failed: %s", result.error)
        return result.success
    except Exception as exc:
        guilogger.warning(f"compact_context: unexpected error: {exc}")
        try:
            _bus = getattr(orch, "event_bus", None)
            if _bus:
                _bus.publish_typed(ContextCompactFailed(message=f"Context compaction failed: {exc}"))
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Step transactions (rollback)
# ---------------------------------------------------------------------------


def begin_step_transaction_impl(orch: Any) -> str:
    """Start a step-level atomic transaction.

    Creates a new snapshot group for the current execution step.
    All files written during this step are accumulated into this snapshot.
    Call rollback_step_transaction_impl() to undo all writes in the step.

    Returns:
        The step snapshot ID.
    """
    step_id = "step_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    orch._step_snapshot_id = step_id
    guilogger.debug(f"begin_step_transaction: started {step_id}")
    # Record timing of begin_step_transaction (very fast, but useful overall)
    try:
        start_ts = time.time()
        if getattr(orch, "working_dir", None):
            from src.tools.tools_config import agent_context_path

            agent_context_dir = agent_context_path(orch.working_dir)
            timings_path = agent_context_dir / "timings.json"
            entry = {
                "phase": "begin_step_transaction",
                "elapsed": 0.0,
                "ts": int(start_ts),
                "step_id": step_id,
            }
            if timings_path.exists():
                try:
                    data = json.loads(timings_path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        data.append(entry)
                    else:
                        data = [data, entry]
                except Exception:
                    data = [entry]
            else:
                data = [entry]
            try:
                timings_path.parent.mkdir(parents=True, exist_ok=True)
                from src.core.io_utils import atomic_write_json
                atomic_write_json(timings_path, data, logger=guilogger)
            except Exception:
                pass
    except Exception:
        pass
    return step_id


def rollback_step_transaction_impl(orch: Any) -> dict:
    """Rollback all writes made during the current step transaction.

    Restores every file that was written since begin_step_transaction() was called.

    Returns:
        Rollback result dict with keys ok, restored_files, restored_count.
    """
    if not orch._step_snapshot_id:
        return {"ok": False, "error": "No active step transaction"}

    snap_id = orch._step_snapshot_id
    result = orch.rollback_manager.rollback(snap_id)
    if result.get("ok"):
        guilogger.info(
            f"rollback_step_transaction: restored {result.get('restored_count', 0)} "
            f"file(s) from step {snap_id}"
        )
    else:
        guilogger.warning(
            f"rollback_step_transaction: rollback failed for {snap_id}: "
            f"{result.get('error')}"
        )
    orch._step_snapshot_id = None
    return result


def commit_step_transaction_impl(orch: Any) -> None:
    """Commit (clean up) the current step transaction.

    Called after verification passes.  Removes the snapshot data associated
    with the current step so it does not accumulate in memory and on disk.
    """
    snap_id = getattr(orch, "_step_snapshot_id", None)
    if not snap_id:
        guilogger.debug("commit_step_transaction: no active step transaction — nothing to commit")
        return
    try:
        if hasattr(orch, "rollback_manager") and orch.rollback_manager is not None:
            orch.rollback_manager.commit_step(snap_id)
            guilogger.debug("commit_step_transaction: committed snapshot %s", snap_id)
    except Exception:
        guilogger.debug(
            "commit_step_transaction: commit failed for %s (non-fatal, snapshot may leak)",
            snap_id,
        )
    finally:
        orch._step_snapshot_id = None


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------


def _get_tool_timeout_impl(orch: Any, tool_name: str) -> int:
    """Get timeout in seconds for a tool. T9: Tool Timeout Protection.

    Delegates to :mod:`src.core.orchestration.tool_registry` so the
    authoritative timeout map lives in one place (ARCH-1).
    """
    try:
        from src.core.orchestration.tool_registry import get_tool_timeout as _gtt

        return _gtt(tool_name)
    except Exception as _e:
        # tool_registry unavailable (e.g. during unit-test isolation); use safe default.
        logger.warning("Could not load tool timeout for '%s' from tool_registry (using default 30s): %s", tool_name, _e)
        return 30


def _normalize_tool_result_impl(orch: Any, res: Any) -> Dict[str, Any]:
    """Ensure tool results conform to a minimal contract.

    Accepts various return shapes and normalizes to a dict with either 'status' or 'ok'.
    """
    try:
        if isinstance(res, dict):
            return res
        return {"status": "ok", "result": res}
    except Exception:
        return {"status": "error", "error": "tool result normalization failed"}


# ---------------------------------------------------------------------------
# Session / snapshot helpers
# ---------------------------------------------------------------------------


def _flush_usage_buffer_impl(orch: Any) -> None:
    """F17: Legacy flush method — delegates to SessionCostTracker for backward compatibility.

    .. deprecated::
        Call ``orch.cost_tracker.flush()`` directly.  This wrapper exists only to
        avoid breaking any external callers.  Internal call sites in
        ``run_agent_once()`` now call ``cost_tracker.flush()`` directly.
    """
    orch.cost_tracker.flush(task_id=getattr(orch, "_current_task_id", ""))


def _create_session_snapshot_impl(orch: Any) -> None:
    """Create a session snapshot for resume capability.

    Delegates to :meth:`SessionManager.create_snapshot`.
    """
    if not hasattr(orch, "session_mgr"):
        return
    orch.session_mgr.create_snapshot(usage_buffer=getattr(orch, "_usage_buffer", None))


# ---------------------------------------------------------------------------
# Working directory scaffolding
# ---------------------------------------------------------------------------


def _ensure_working_dir_impl(orch: Any) -> None:
    """Ensure the working directory and .localAgent scaffold exist."""
    start_ts = time.time()
    try:
        orch.working_dir.mkdir(parents=True, exist_ok=True)

        # Phase 3: Scaffold per-workspace context directory (uses tools_config)
        try:
            from src.tools.tools_config import agent_context_path

            agent_context_dir = agent_context_path(orch.working_dir)
        except Exception:
            agent_context_dir = orch.working_dir / ".codingAgent"
            agent_context_dir.mkdir(parents=True, exist_ok=True)

        task_state_path = agent_context_dir / "TASK_STATE.md"
        if not task_state_path.exists():
            # Write atomically via mkstemp -> replace to avoid partial files
            try:
                fd, tmp = tempfile.mkstemp(
                    dir=str(task_state_path.parent), suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write("# Current Task\n\n# Completed Steps\n\n# Next Step\n")
                    try:
                        shutil.move(tmp, str(task_state_path))
                    except Exception:
                        os.replace(tmp, str(task_state_path))
                except Exception:
                    try:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                    except Exception:
                        pass
                    raise
            except Exception:
                try:
                    task_state_path.write_text(
                        "# Current Task\n\n# Completed Steps\n\n# Next Step\n"
                    )
                except Exception:
                    guilogger.debug(
                        "orchestrator_helpers: failed to write TASK_STATE.md fallback: %s",
                        traceback.format_exc(),
                    )

        active_path = agent_context_dir / "ACTIVE.md"
        if not active_path.exists():
            try:
                fd, tmp = tempfile.mkstemp(dir=str(active_path.parent), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write("No active goal.")
                    try:
                        os.replace(tmp, str(active_path))
                    except Exception:
                        shutil.move(tmp, str(active_path))
                except Exception:
                    try:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                    except Exception:
                        pass
                    raise
            except Exception:
                try:
                    active_path.write_text("No active goal.")
                except Exception:
                    guilogger.debug(
                        "orchestrator_helpers: failed to write ACTIVE.md fallback: %s",
                        traceback.format_exc(),
                    )

        trace_path = agent_context_dir / "execution_trace.json"
        if not trace_path.exists():
            try:
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                from src.core.io_utils import atomic_write_json
                if not atomic_write_json(trace_path, [], logger=guilogger):
                    trace_path.write_text(json.dumps([]))
            except Exception:
                try:
                    trace_path.write_text(json.dumps([]))
                except Exception:
                    guilogger.debug(
                        "orchestrator_helpers: failed to write trace_path fallback: %s",
                        traceback.format_exc(),
                    )

    except Exception as e:
        guilogger.error(
            f"Orchestrator: failed to create working dir {orch.working_dir}: {e}"
        )
        # BUG-VOL22-3: Record the failure on the orchestrator so callers can check
        # whether the working directory is usable before dispatching file tools.
        # We do NOT re-raise here because _ensure_working_dir_impl is called from
        # __init__ (via orchestrator_bootstrap), and raising would prevent the
        # orchestrator from constructing at all — worse than a degraded state.
        try:
            orch._working_dir_unavailable = True
            _bus = getattr(orch, "event_bus", None)
            if _bus:
                _bus.publish_typed(WorkingDirUnavailable(path=str(orch.working_dir), error=str(e)))
        except Exception:
            pass
    finally:
        # Best-effort timing/logging for diagnostics
        try:
            elapsed = time.time() - start_ts
            try:
                from src.tools.tools_config import agent_context_path

                agent_context_dir = agent_context_path(orch.working_dir)
            except Exception:
                agent_context_dir = orch.working_dir / ".codingAgent"
                agent_context_dir.mkdir(parents=True, exist_ok=True)
            timings_path = agent_context_dir / "timings.json"
            entry = {
                "phase": "ensure_working_dir",
                "elapsed": elapsed,
                "ts": int(start_ts),
            }
            if timings_path.exists():
                try:
                    data = json.loads(timings_path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        data.append(entry)
                    else:
                        data = [data, entry]
                except Exception:
                    data = [entry]
            else:
                data = [entry]
            try:
                timings_path.parent.mkdir(parents=True, exist_ok=True)
                from src.core.io_utils import atomic_write_json
                atomic_write_json(timings_path, data, logger=guilogger)
            except Exception:
                pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Background model check
# ---------------------------------------------------------------------------


def _background_model_check_impl(orch: Any) -> None:
    """Check for available models in the background (non-blocking)."""
    start_ts = time.time()
    # Guarded import to avoid circular imports during tests or when the
    _get_provider_manager: Any = None
    # provider manager module is unavailable in isolated environments.
    try:
        from src.core.inference.llm_manager import get_provider_manager as _get_provider_manager  # type: ignore[assignment]
    except Exception:
        pass

    try:
        if _get_provider_manager is None:
            return
        pm = None
        try:
            pm = _get_provider_manager()
        except Exception:
            pm = None

        if pm:
            # First try to use cached models to avoid redundant API calls
            cached = None
            try:
                cached = pm.get_cached_models("lm_studio")
            except Exception:
                pass

            # Only call API if no cached models available
            if not cached:
                try:
                    adapters = pm.list_providers()
                    if "lm_studio" in adapters:
                        ad = pm.get_provider("lm_studio")
                        if ad and hasattr(ad, "get_models_from_api"):
                            ad.get_models_from_api()
                except Exception:
                    pass

            try:
                orch.event_bus.publish_typed(ProviderModelsCached(provider="lm_studio"))
                orch.event_bus.publish(
                    "provider.models.probing.completed", {"provider": "lm_studio"}
                )
            except Exception:
                pass
    except Exception as _e:
        guilogger.warning(f"background_model_check failed: {_e}")
    finally:
        # Record timing for provider/model check
        try:
            elapsed = time.time() - start_ts
            if getattr(orch, "working_dir", None):
                try:
                    from src.tools.tools_config import agent_context_path

                    agent_context_dir = agent_context_path(orch.working_dir)
                except Exception:
                    agent_context_dir = orch.working_dir / ".codingAgent"
                    agent_context_dir.mkdir(parents=True, exist_ok=True)

                timings_path = agent_context_dir / "timings.json"
                entry = {
                    "phase": "background_model_check",
                    "elapsed": elapsed,
                    "ts": int(start_ts),
                }
                if timings_path.exists():
                    try:
                        data = json.loads(timings_path.read_text(encoding="utf-8"))
                        if isinstance(data, list):
                            data.append(entry)
                        else:
                            data = [data, entry]
                    except Exception:
                        data = [entry]
                else:
                    data = [entry]
                try:
                    timings_path.parent.mkdir(parents=True, exist_ok=True)
                    from src.core.io_utils import atomic_write_json
                    atomic_write_json(timings_path, data, logger=guilogger)
                except Exception:
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Git status publishing
# ---------------------------------------------------------------------------


def _publish_git_status_impl(orch: Any) -> None:
    """TUI-07: Publish git.branch event so the TUI sidebar stays current.

    Called from start_new_task() and after run_agent_once() completes.
    All subprocess errors are silently swallowed — git status is informational.
    """
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=str(orch.working_dir),
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return  # not a git repo or git not available

    try:
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=str(orch.working_dir),
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
        dirty = bool(status_out.strip())
    except Exception:
        dirty = False

    # Parse ahead/behind counts from `git status -sb` if possible
    ahead = 0
    behind = 0
    try:
        sb_out = subprocess.check_output(
            ["git", "status", "-sb"],
            cwd=str(orch.working_dir),
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
        m = re.search(r"\[ahead (\d+)(?:, behind (\d+))?\]", sb_out)
        if not m:
            m = re.search(r"\[behind (\d+)\]", sb_out)
            if m:
                behind = int(m.group(1))
        else:
            ahead = int(m.group(1))
            if m.group(2):
                behind = int(m.group(2))
    except Exception:
        pass

    try:
        orch.event_bus.publish_typed(GitBranch(branch=branch, dirty=dirty, ahead=ahead, behind=behind))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


async def start_mcp_server_impl(orch: Any) -> None:
    """Start the MCP STDIO server for IDE integration (JSON-RPC over stdin/stdout)."""
    try:
        from src.core.orchestration.mcp_stdio_server import MCPStdioServer

        orch._mcp_server = MCPStdioServer(orchestrator=orch)
        logger.info("Orchestrator: starting MCP STDIO server")
        try:
            orch.event_bus.publish_typed(McpServerStatus(running=True, count=1))
        except Exception:
            pass
        await orch._mcp_server.run_async()
    except Exception as e:
        logger.error(f"Orchestrator: MCP STDIO server error: {e}")
    finally:
        orch._mcp_server = None
        try:
            orch.event_bus.publish_typed(McpServerStatus(running=False, count=0))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Budget status
# ---------------------------------------------------------------------------


def get_budget_status_impl(orch: Any, session_id: str = "default") -> Dict[str, Any]:
    """Get current token budget status for UI display."""
    budget = orch.token_monitor.get_budget(session_id)
    context_status = orch.context_controller.get_budget_status()
    return {
        "token_budget": {
            "used_tokens": budget.used_tokens,
            "max_tokens": budget.max_tokens,
            "usage_ratio": budget.usage_ratio,
            "should_compact": budget.should_compact,
            "should_warn": budget.should_warn,
            "current_turn": budget.current_turn,
        },
        "context_budget": context_status,
        "usage_ratio": budget.usage_ratio,
        "used_tokens": budget.used_tokens,
        "max_tokens": budget.max_tokens,
    }
