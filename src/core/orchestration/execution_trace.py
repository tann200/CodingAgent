"""Execution trace management for loop detection and audit trail.

Extracted from orchestrator.py (Phase G2) — single responsibility.
All functions take ``orch`` as the first argument (the Orchestrator instance).
"""

from __future__ import annotations

import datetime
import json
import logging
import traceback
from pathlib import Path
from typing import Any, Optional

from src.core.logger import logger as guilogger

logger = logging.getLogger(__name__)


def _resolve_agent_context_dir(orch: Any) -> Path:
    """Resolve the per-workspace agent context directory for the orchestrator.

    Prefer src.tools.tools_config.agent_context_path when available; fall back
    to the legacy ".agent-context" directory next to orch.working_dir.
    """
    # Guard: orch.working_dir may be a string or Path; callers should ensure it's set
    wd = Path(orch.working_dir)
    try:
        from src.tools.tools_config import agent_context_path

        return agent_context_path(wd)
    except Exception:
        return wd / ".agent-context"


def _read_execution_trace_impl(orch: Any) -> list:
    """Read execution trace from disk."""
    # BUG-FIX: guard against None working_dir
    if not orch.working_dir:
        return []
    try:
        trace_path = _resolve_agent_context_dir(orch) / "execution_trace.json"
        if trace_path.exists():
            return json.loads(trace_path.read_text())
    except Exception:
        pass
    return []


def _normalize_args_impl(orch: Any, a: Any) -> Any:
    """Normalize args into a JSON-serializable Python structure.

    Ensures consistent comparison in loop detection regardless of
    original arg types (Path, objects, etc.).
    """
    try:
        return json.loads(json.dumps(a, default=str))
    except Exception:
        try:
            return str(a)
        except Exception:
            return None


def _append_execution_trace_impl(orch: Any, entry: dict) -> None:
    """Buffer execution trace entries in memory; flush to disk via flush_execution_trace.

    PB-4 fix: previously every tool call wrote the full trace JSON to disk synchronously,
    causing O(n) disk I/O per tool where n is trace length.  Now entries are kept in
    orch._execution_trace_buffer and flushed once per task by run_agent_once().
    """
    try:
        # Initialise buffer on first use (handles pickled/restored instances too)
        if not hasattr(orch, "_execution_trace_buffer"):
            orch._execution_trace_buffer = []  # type: ignore[attr-defined]

        # Compute retry count from recent buffer + persisted trace
        recent = orch._execution_trace_buffer[-10:]
        count = 0
        for e in recent:
            try:
                if e.get("tool") == entry.get("tool") and e.get(
                    "args"
                ) == _normalize_args_impl(orch, entry.get("args")):
                    count += 1
            except Exception:
                if e.get("tool") == entry.get("tool") and e.get("args") == entry.get(
                    "args"
                ):
                    count += 1
        entry["retries"] = count
        try:
            entry["args"] = _normalize_args_impl(orch, entry.get("args"))
        except Exception:
            pass
        orch._execution_trace_buffer.append(entry)
    except Exception as e:
        guilogger.error(f"Orchestrator: failed to buffer execution trace entry: {e}")


def flush_execution_trace_impl(orch: Any) -> None:
    """Flush buffered trace entries to disk once per task (called by run_agent_once)."""
    if not hasattr(orch, "_execution_trace_buffer") or not orch._execution_trace_buffer:
        return
    # BUG-FIX #6: guard against None working_dir
    if not orch.working_dir:
        return
    try:
        trace = _read_execution_trace_impl(orch)
        trace.extend(orch._execution_trace_buffer)
        orch._execution_trace_buffer = []
        # HR-2 fix: cap trace at 2000 entries to prevent unbounded file growth.
        # Loop detection only looks at a 300-second window, so older entries are stale.
        _TRACE_MAX = 2000
        if len(trace) > _TRACE_MAX:
            trace = trace[-_TRACE_MAX:]
        trace_path = _resolve_agent_context_dir(orch) / "execution_trace.json"

        def serializer(obj: Any) -> str:
            if isinstance(obj, Path):
                return str(obj)
            return str(obj)

        # Ensure parent directory exists immediately before writing
        trace_path.parent.mkdir(parents=True, exist_ok=True)

        # Prefer central atomic writer; fall back to write_text
        try:
            from src.core.io_utils import atomic_write_json

            guilogger.debug(
                "execution_trace: attempting atomic_write_json for %s", trace_path
            )
            ok = atomic_write_json(trace_path, trace, logger=guilogger)
            if ok:
                guilogger.info("Execution trace written atomically: %s", trace_path)
                return
            guilogger.warning(
                "execution_trace: atomic_write_json returned False for %s; falling back",
                trace_path,
            )
        except Exception:
            guilogger.debug(
                "execution_trace: atomic_write_json unavailable or failed for %s; falling back\n%s",
                trace_path,
                traceback.format_exc(),
            )

        # Fallback: write via a unique-temp + atomic replace to avoid
        # exposing partially-written JSON to readers.
        try:
            import tempfile
            import os
            import shutil

            fd = None
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=str(trace_path.parent), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    fd = None
                    json.dump(trace, f, indent=2, default=serializer)
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                try:
                    os.replace(tmp, str(trace_path))
                except Exception:
                    try:
                        shutil.move(tmp, str(trace_path))
                    except Exception:
                        pass
            finally:
                try:
                    if fd is not None:
                        os.close(fd)
                except Exception:
                    pass
        except Exception as e:
            guilogger.error(f"Orchestrator: failed to flush execution trace: {e}")
    except Exception as e:
        guilogger.error(f"Orchestrator: failed to flush execution trace: {e}")


def _clear_execution_trace_impl(orch: Any) -> None:
    """Clear the in-memory buffer and reset the on-disk trace file."""
    # Also clear the in-memory buffer so buffered entries don't reappear after clear
    if hasattr(orch, "_execution_trace_buffer"):
        orch._execution_trace_buffer = []
    try:
        trace_path = _resolve_agent_context_dir(orch) / "execution_trace.json"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from src.core.io_utils import atomic_write_json

            guilogger.debug(
                "execution_trace: attempting atomic_write_json to clear %s", trace_path
            )
            ok = atomic_write_json(trace_path, [], logger=guilogger)
            if ok:
                guilogger.info("Execution trace cleared atomically: %s", trace_path)
                return
            guilogger.warning(
                "execution_trace: atomic_write_json returned False while clearing %s; falling back",
                trace_path,
            )
        except Exception:
            guilogger.debug(
                "execution_trace: atomic_write_json unavailable or failed while clearing %s; falling back\n%s",
                trace_path,
                traceback.format_exc(),
            )

        # mkstemp -> os.replace fallback for JSON write
        try:
            import tempfile
            import os
            import shutil

            fd = None
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=str(trace_path.parent), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    fd = None
                    json.dump([], f, indent=2)
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                try:
                    os.replace(tmp, str(trace_path))
                except Exception:
                    shutil.move(tmp, str(trace_path))
            finally:
                try:
                    if fd is not None:
                        os.close(fd)
                except Exception:
                    pass
        except Exception as e:
            guilogger.error(f"Orchestrator: failed to clear execution trace: {e}")
    except Exception as e:
        guilogger.error(f"Orchestrator: failed to clear execution trace: {e}")


def _check_loop_prevention_impl(
    orch: Any, tool_name: Optional[str], tool_args: dict
) -> bool:
    """Check whether the same tool+args have been called too many times recently."""
    if not tool_name:
        return False
    # Load trace and apply a time-windowed, conservative de-duplication strategy.
    # Rationale: block only when the same tool+args is repeatedly attempted within a
    # short timeframe (e.g., 5 minutes) and with at least 3 attempts — this reduces
    # false-positives while still protecting against runaway loops.
    try:
        trace = _read_execution_trace_impl(orch) or []
    except Exception:
        trace = []

    # PB-4: merge in-memory buffer so loop detection works even before flush
    buffer = getattr(orch, "_execution_trace_buffer", [])
    if buffer:
        trace = trace + list(buffer)

    if not trace:
        return False

    # Consider only recent entries within TIME_WINDOW seconds
    TIME_WINDOW = 300
    now_ts = None
    recent_entries = []
    try:
        now_ts = datetime.datetime.now(datetime.timezone.utc)
        for e in reversed(trace):
            ts = e.get("ts")
            if not ts:
                # If no timestamp, include but don't rely on it for timing
                recent_entries.append(e)
                continue
            try:
                entry_ts = datetime.datetime.fromisoformat(ts)
            except Exception:
                # try parsing without timezone
                try:
                    entry_ts = datetime.datetime.fromisoformat(ts + "+00:00")
                except Exception:
                    recent_entries.append(e)
                    continue
            delta = (now_ts - entry_ts).total_seconds()
            if delta <= TIME_WINDOW:
                recent_entries.append(e)
            else:
                break
    except Exception:
        recent_entries = trace[-10:]

    # MED-6 fix: the comment said "3+ matches" but the threshold was `>= 2`
    # (triggers on the 2nd match).  Align threshold with the stated intent.
    # Now count exact matches (tool + args) conservatively: block only if 3+ matches
    exact_count = 0
    for entry in recent_entries:
        try:
            if entry.get("tool") == tool_name and entry.get(
                "args"
            ) == _normalize_args_impl(orch, tool_args):
                exact_count += 1
        except Exception:
            continue
    if exact_count >= 3:
        return True

    # Count tool-only occurrences and require a higher threshold (e.g., 6) to block
    tool_only_count = 0
    for entry in recent_entries:
        try:
            if entry.get("tool") == tool_name:
                tool_only_count += 1
        except Exception:
            continue
    if tool_only_count >= 6:
        return True

    return False
