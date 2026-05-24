from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import logging as _logging

_helpers_logger = _logging.getLogger(__name__)


def maybe_inject_repo_overview(
    working_dir: str,
    relevant_files: List[str],
    analysis_summary: str,
) -> str:
    """Return analysis_summary enriched with repo_overview tree when no files known."""
    if relevant_files:
        return analysis_summary
    try:
        from src.tools.repo_read_tools import repo_overview as _ov_fn
        ov = _ov_fn(workdir=working_dir, max_depth=3, max_files=200)
        if ov.get("ok") and ov.get("entries"):
            lines = [f"{'  '*(e['depth']-1)}{e['path']} ({'dir' if e['type']=='dir' else 'file'})"
                     for e in ov["entries"][:80]]
            manifests = ", ".join(ov.get("manifests") or []) or "none"
            _helpers_logger.debug("planning_helpers: repo_overview injected (%d entries)", len(ov["entries"]))
            return (f"[repo_overview] root={ov['root']} manifests={manifests} "
                    f"truncated={ov['truncated']}\n" + "\n".join(lines))
    except Exception as exc:
        _helpers_logger.debug("planning_helpers: repo_overview skipped: %s", exc)
    return analysis_summary


def resolve_planning_orchestrator(
    *,
    state: Mapping[str, Any],
    config: Any,
    plan_attempts: int,
    resolve_orchestrator_fn: Callable[[Mapping[str, Any], Any], Any],
    build_planning_error_result_fn: Callable[..., Dict[str, Any]],
    logger: Any,
) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """Resolve the planning orchestrator and normalize early config errors."""
    try:
        orchestrator = resolve_orchestrator_fn(state, config)
        if orchestrator is not None:
            return orchestrator, None

        logger.error("planning_node: orchestrator is None")
        return None, build_planning_error_result_fn(
            current_plan=state.get("current_plan", []),
            current_step=state.get("current_step", 0),
            plan_attempts=plan_attempts,
            errors=["orchestrator not found"],
        )
    except Exception as exc:
        logger.error("planning_node: failed to get orchestrator: %s", exc)
        return None, build_planning_error_result_fn(
            current_plan=state.get("current_plan", []),
            current_step=state.get("current_step", 0),
            plan_attempts=plan_attempts,
            errors=[f"config error: {exc}"],
        )


def get_last_plan_path(*, workdir: str, agent_context_path_fn: Optional[Callable[[Path], Path]] = None) -> Path:
    """Return the persisted last-plan path for a working directory."""
    if agent_context_path_fn is not None:
        try:
            return agent_context_path_fn(Path(workdir)) / "last_plan.json"
        except Exception:
            pass
    return Path(workdir) / ".codingAgent" / "last_plan.json"


def load_last_plan(*, workdir: str, get_last_plan_path_fn: Callable[[str], Path], logger: Any) -> Dict[str, Any]:
    """Load the last persisted plan, returning an empty dict when unavailable."""
    plan_path = get_last_plan_path_fn(workdir)
    if plan_path.exists():
        try:
            data = json.loads(plan_path.read_text())
            logger.info("planning_node: loaded last plan from %s", plan_path)
            return data
        except Exception as exc:
            logger.warning("planning_node: failed to load last plan: %s", exc)
    return {}


def save_last_plan(
    *,
    workdir: str,
    plan: list,
    task: str,
    step: int,
    get_last_plan_path_fn: Callable[[str], Path],
    logger: Any,
    atomic_write_json_fn: Optional[Callable[..., bool]] = None,
    now_fn: Callable[[], datetime] = datetime.now,
) -> None:
    """Persist the latest plan with atomic-write and mkstemp fallbacks."""
    plan_path = get_last_plan_path_fn(workdir)
    try:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "plan": plan,
            "task": task,
            "current_step": step,
            "working_dir": str(workdir),
            "saved_at": now_fn().isoformat(),
        }
        try:
            if atomic_write_json_fn is None:
                from src.core.io_utils import atomic_write_json as _atomic_write_json

                atomic_write_json_fn = _atomic_write_json

            logger.debug("planning_node: attempting atomic_write_json for %s", plan_path)
            ok = atomic_write_json_fn(plan_path, data, logger=logger)
            if ok:
                logger.info("planning_node: saved plan to %s", plan_path)
                return
            logger.warning(
                "planning_node: atomic_write_json returned False for %s; falling back",
                plan_path,
            )
        except Exception:
            logger.debug(
                "planning_node: atomic_write_json unavailable or failed for %s; falling back\n%s",
                plan_path,
                traceback.format_exc(),
            )

        fd, tmp_path = tempfile.mkstemp(dir=str(plan_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                except Exception:
                    pass
            try:
                os.replace(tmp_path, str(plan_path))
            except Exception:
                try:
                    shutil.move(tmp_path, str(plan_path))
                except Exception:
                    logger.warning(
                        "planning_node: mkstemp fallback failed for %s; final fallback to write_text",
                        plan_path,
                    )
                    plan_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            try:
                if fd:
                    os.close(fd)
            except Exception:
                pass
            raise
    except Exception as exc:
        logger.warning("planning_node: failed to save last plan: %s", exc)


def append_plan_audit_log(
    *,
    workdir: str,
    plan: list,
    task: str,
    step: int,
    get_last_plan_path_fn: Callable[[str], Path],
    logger: Any,
    now_fn: Callable[[], datetime] = datetime.now,
) -> None:
    """Append a plan snapshot to the append-only plan_history.jsonl audit log.

    The log lives at ``<agent_context_dir>/plan_history.jsonl`` and is never
    truncated — every planning decision is preserved for post-hoc analysis.
    Failures are logged at DEBUG level and swallowed so they never block execution.
    """
    try:
        plan_path = get_last_plan_path_fn(workdir)
        history_path = plan_path.parent / "plan_history.jsonl"
        entry = {
            "ts": now_fn().isoformat(),
            "task": task,
            "step": step,
            "plan_len": len(plan),
            "plan": plan,
        }
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        logger.debug("planning_node: appended plan to audit log %s", history_path)
    except Exception as exc:
        logger.debug("planning_node: plan audit log write skipped: %s", exc)


def plan_is_resumable(
    *,
    data: Dict[str, Any],
    current_task: str,
    resume_ttl_seconds: int,
    logger: Any,
    now_fn: Callable[[], datetime] = datetime.now,
    datetime_fromisoformat_fn: Callable[[str], datetime] = datetime.fromisoformat,
    resume_session: bool = False,
) -> bool:
    """Return True when a saved plan should be resumed instead of regenerated."""
    if resume_session:
        return bool(data.get("plan"))
    if data.get("task", "") != current_task:
        return False
    saved_at_str = data.get("saved_at", "")
    try:
        saved_dt = datetime_fromisoformat_fn(saved_at_str)
        age_seconds = (now_fn() - saved_dt).total_seconds()
        if age_seconds > resume_ttl_seconds:
            logger.info(
                "planning_node: saved plan is %.0fs old (> %ss TTL) — not resuming",
                age_seconds,
                resume_ttl_seconds,
            )
            return False
    except Exception:
        return False
    return True


def hydrate_repo_context_from_index(
    *,
    working_dir: str,
    task: str,
    relevant_files: Sequence[str],
    key_symbols: Sequence[str],
    get_symbols_for_task_fn: Callable[..., Sequence[Mapping[str, Any]]],
    logger: Any,
    max_results: int = 8,
    max_files: int = 10,
    max_symbols: int = 10,
) -> Tuple[list[str], list[str], list[dict[str, Any]]]:
    """Best-effort repo-index fallback for planning context."""
    indexed_symbols: list[dict[str, Any]] = []
    hydrated_files = list(relevant_files)
    hydrated_symbols = list(key_symbols)

    if not working_dir or not task or (hydrated_files and hydrated_symbols):
        return hydrated_files, hydrated_symbols, indexed_symbols

    try:
        indexed_symbols = list(get_symbols_for_task_fn(working_dir, task, max_results=max_results) or [])
        if indexed_symbols:
            if not hydrated_files:
                hydrated_files = list(
                    dict.fromkeys(
                        str(sym.get("file_path"))
                        for sym in indexed_symbols
                        if sym.get("file_path")
                    )
                )[:max_files]
            if not hydrated_symbols:
                hydrated_symbols = list(
                    dict.fromkeys(
                        str(sym.get("name"))
                        for sym in indexed_symbols
                        if sym.get("name")
                    )
                )[:max_symbols]
            logger.info(
                "planning_node: hydrated repo context from index (%d files, %d symbols)",
                len(hydrated_files),
                len(hydrated_symbols),
            )
    except Exception as exc:
        logger.debug(
            "planning_node: repo-context hydration failed (non-critical): %s",
            exc,
        )

    return hydrated_files, hydrated_symbols, indexed_symbols
