"""change_log.py — P3.2 live repo-change audit trail.

Answers the question the agent could not previously answer: **which files did
this session change, and why**.  Before this module the only artefacts were

* ``orch._session_modified_files`` — an in-memory ``set`` of absolute paths,
  consumed only by ``work_summary`` to render a git diff at task end, and lost
  on crash.  It records *what*, never *why*.
* ``execution_trace.json`` — a tool-call trace (``tool``/``args``/``retries``)
  flushed once per task, so a mid-task crash loses every entry, and the paths a
  tool actually changed are never reconciled against the plan.
* ``affected_files`` in graph state — the plan's *intent*, never checked against
  what landed on disk.

This module records one durable entry per file a modifying tool call actually
changed, with before/after fingerprints and the plan-step context in force at
the time, appending to ``<agent context>/change_log.jsonl`` and publishing a
typed ``RepoChangeRecorded`` event for live subscribers.

Design constraints:

* **Fingerprints, not git.** The trail must work in a non-git workspace (a
  large share of real use), and must attribute *individual writes* rather than a
  single end-of-task diff.  Git remains the end-of-task diff view
  (``work_summary``); this is the durable, attributed, per-write record.
* **Never raises.** The journal is observability, not correctness: every entry
  point is fully guarded so a broken trail can never fail a tool call.
* **Reconciled, not optimistic.** An entry is only written for a path whose
  fingerprint actually moved, so a tool that reported success but changed
  nothing leaves no phantom record — and one that failed to change anything is
  still visible, which is exactly the case worth auditing.
* **Alias-normalised and confined.** Paths come from
  ``_path_utils.changed_paths`` (shared with the repo index refresh) and are
  always recorded relative to the working directory when they live inside it.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: Journal filename inside the agent-context directory.
JOURNAL_NAME = "change_log.jsonl"

#: Rotate the journal once it exceeds this many entries (keeps the newest).
MAX_JOURNAL_ENTRIES = 5000

#: Content above this size is fingerprinted by size instead of hashed, so a
#: multi-hundred-megabyte artifact cannot make every write pay a full read.
_MAX_DIGEST_BYTES = 1 << 20

#: Cap on a journal field that could hold model-authored text (the reason).
_MAX_REASON_CHARS = 300

# Appends are serialised per-process so concurrent tool threads cannot
# interleave partial lines.  Cross-process safety comes from append-mode
# single-line writes, which are atomic for small records on POSIX.
_JOURNAL_LOCK = threading.Lock()

#: Change kinds recorded in the journal.
CREATED = "created"
MODIFIED = "modified"
UNCHANGED = "unchanged"
DELETED = "deleted"


# ---------------------------------------------------------------------------
# Attribution context
# ---------------------------------------------------------------------------


def set_change_context(orch: Any, state: Mapping[str, Any]) -> None:
    """Publish the current plan context onto *orch* for later attribution.

    Called once per round by the inference loop (the plan can only change
    between graph invocations, not between tool calls inside one loop), so the
    tool-call sites stay a pure lookup.  Best-effort: a state shape we do not
    understand simply clears the context.
    """
    try:
        plan = state.get("current_plan") or []
        step = state.get("current_step")
        step_desc = ""
        if isinstance(step, int) and 0 <= step < len(plan):
            candidate = plan[step]
            if isinstance(candidate, Mapping):
                step_desc = str(candidate.get("description") or "")
        task = str(state.get("task") or "")[:_MAX_REASON_CHARS]
        reason_parts = []
        if isinstance(step, int):
            reason_parts.append(f"step {step + 1}/{len(plan)}" if plan else f"step {step + 1}")
        if step_desc:
            reason_parts.append(step_desc)
        if not reason_parts:
            reason_parts.append(task or "unattributed")
        context = {
            "task_id": getattr(orch, "_current_task_id", None) or "",
            "step": step if isinstance(step, int) else None,
            "step_total": len(plan) if isinstance(plan, list) else 0,
            "reason": " — ".join(reason_parts)[:_MAX_REASON_CHARS],
            "planned_files": [
                str(f) for f in (state.get("affected_files") or []) if isinstance(f, str)
            ],
        }
        orch._change_context = context
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("change_log: set_change_context skipped: %s", exc)


def _context(orch: Any) -> Dict[str, Any]:
    """Return the orchestrator's change context, defensively."""
    ctx = getattr(orch, "_change_context", None)
    return ctx if isinstance(ctx, dict) else {}


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def fingerprint(path: Path) -> Tuple[bool, int, str]:
    """Return ``(exists, size_bytes, digest)`` for *path*.

    ``exists`` is False for a missing path (digest is then empty).  Directories
    report the literal ``"dir"`` digest — they can be renamed but not rewritten,
    and hashing one would be meaningless.

    Never raises: an unreadable file reports ``"unreadable"`` rather than
    propagating, so a permission error cannot break the tool that caused it.
    """
    try:
        stat_result = path.stat()
    except OSError:
        return (False, 0, "")
    try:
        if path.is_dir():
            return (True, 0, "dir")
    except OSError:
        pass
    if stat_result.st_size > _MAX_DIGEST_BYTES:
        return (True, stat_result.st_size, f"size:{stat_result.st_size}")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return (True, stat_result.st_size, "unreadable")
    return (True, stat_result.st_size, digest)


def _display_path(path: Path, workdir: Path) -> Tuple[str, bool]:
    """Return ``(display_path, outside_workdir)``.

    Paths inside the working directory are recorded relative to it so the
    journal is portable and diffable; anything outside keeps its absolute form
    and is flagged, because an out-of-tree write is worth surfacing in an audit.
    """
    resolved = path
    try:
        resolved = path.resolve()
    except OSError:
        pass
    try:
        base = workdir.resolve()
    except OSError:
        base = workdir
    try:
        return (str(resolved.relative_to(base)), False)
    except ValueError:
        return (str(resolved), True)


def _classify(before: Tuple[bool, int, str], after: Tuple[bool, int, str]) -> str:
    existed_before, _size_before, digest_before = before
    exists_after, _size_after, digest_after = after
    if existed_before and not exists_after:
        return DELETED
    if not existed_before:
        return CREATED
    if digest_before != digest_after:
        return MODIFIED
    return UNCHANGED


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def journal_path(workdir: Any) -> Optional[Path]:
    """Return the journal path for *workdir*, or ``None`` if unresolvable."""
    if not workdir:
        return None
    try:
        from src.tools.tools_config import agent_context_path

        return agent_context_path(Path(str(workdir))) / JOURNAL_NAME
    except Exception as exc:
        logger.debug("change_log: journal path unavailable for %r: %s", workdir, exc)
        return None


def _append_entry(target: Path, entry: Mapping[str, Any]) -> None:
    """Append one JSONL record, rotating the journal when it grows too large."""
    line = json.dumps(entry, default=str)
    with _JOURNAL_LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("change_log: append failed for %s: %s", target, exc)
            return
        try:
            if target.stat().st_size < _ROTATION_BYTES:
                return
        except OSError:
            return
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
            keep = lines[-MAX_JOURNAL_ENTRIES:]
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, target)
        except Exception as exc:
            logger.warning("change_log: rotation failed for %s: %s", target, exc)


#: Rotate on *size* rather than line count: counting lines means reading the
#: whole journal after every single append, which is O(n) per tool call.
_ROTATION_BYTES = 2 << 20


def read_change_log(workdir: Any, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read journal entries oldest-first, optionally limited to the newest *limit*.

    A corrupt or missing journal yields ``[]`` — an unreadable audit trail must
    not break the caller.  Never raises.
    """
    target = journal_path(workdir)
    if target is None or not target.exists():
        return []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("change_log: read failed for %s: %s", target, exc)
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    if limit is not None and limit >= 0:
        out = out[-limit:] if limit else []
    return out


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _modifying_paths(tool_name: Any, tool_args: Any) -> List[str]:
    """Return the paths of a *modifying* tool call, alias-normalised.

    Membership comes from the canonical ``MODIFYING_TOOLS`` set (the same one
    the index refresh and the in-loop checkpoint use), so ``write`` cannot slip
    through as a read and neither can ``read_file`` be credited with creating a
    file it merely opened.  Returns ``[]`` for anything unrecognized.
    """
    if not isinstance(tool_args, dict):
        return []
    name = str(tool_name)
    try:
        from src.tools.constants import MODIFYING_TOOLS
        from src.tools.tools_config import resolve_tool_alias

        canonical = resolve_tool_alias(name)
    except Exception:
        return []
    if canonical not in MODIFYING_TOOLS and name not in MODIFYING_TOOLS:
        return []
    try:
        from src.tools._path_utils import changed_paths

        return changed_paths(canonical, tool_args)
    except Exception as exc:
        logger.debug("change_log: path extraction failed for %r: %s", tool_name, exc)
        return []


def _pending_store(orch: Any) -> Dict[str, List[Tuple[bool, int, str, str]]]:
    """Per-orchestrator map of pre-write fingerprints, keyed by tool+path.

    A list per key (pushed and popped LIFO) rather than a bare value so a nested
    re-entrant tool call with identical arguments cannot steal its parent's
    "before" state.
    """
    store = getattr(orch, "_change_pending", None)
    if not isinstance(store, dict):
        store = {}
        try:
            orch._change_pending = store
        except Exception:
            pass
    return store


def begin_change(orch: Any, tool_name: str, tool_args: Mapping[str, Any], workdir: Any) -> None:
    """Capture the pre-write fingerprint of every path the tool may change.

    Called immediately before the tool runs.  Never raises.

    Skipped for non-modifying tools, so a plain read costs nothing.
    """
    paths = _modifying_paths(tool_name, tool_args)
    if not paths or not workdir:
        return
    base = Path(str(workdir))
    store = _pending_store(orch)
    with _JOURNAL_LOCK:
        for raw in paths:
            key = f"{tool_name}:{raw}"
            try:
                snapshot = fingerprint(base / raw)
            except Exception:
                continue
            store.setdefault(key, []).append((snapshot[0], snapshot[1], snapshot[2], raw))


def record_change(
    orch: Any,
    tool_name: str,
    tool_args: Mapping[str, Any],
    workdir: Any,
) -> Optional[Dict[str, Any]]:
    """Record the changes a completed tool call made; return the first entry.

    Called immediately after the tool runs (successfully or not — a failed
    write that still touched disk is precisely what an audit trail should
    surface).  Returns ``None`` when nothing moved.  Never raises.

    Only paths with a captured "before" fingerprint are considered: without it
    the trail cannot distinguish "the agent created this file" from "the file
    was already there", and guessing would put a phantom creation in the audit
    record.
    """
    paths = _modifying_paths(tool_name, tool_args)
    if not paths or not workdir:
        return None

    base = Path(str(workdir))
    store = _pending_store(orch)
    context = _context(orch)
    planned = {str(f) for f in context.get("planned_files") or []}
    first: Optional[Dict[str, Any]] = None

    for raw in paths:
        key = f"{tool_name}:{raw}"
        with _JOURNAL_LOCK:
            pending = store.get(key) or []
            if not pending:
                continue
            before = pending.pop()
            if not pending:
                store.pop(key, None)
        try:
            after = fingerprint(base / raw)
        except Exception:
            continue
        kind = _classify((before[0], before[1], before[2]), after)
        if kind == UNCHANGED:
            continue
        display, outside = _display_path(base / raw, base)
        entry: Dict[str, Any] = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "task_id": context.get("task_id") or "",
            "tool": str(tool_name),
            "path": display,
            "change": kind,
            "bytes": after[1],
            "digest_before": before[2],
            "digest_after": after[2],
            "reason": context.get("reason") or "unattributed",
            "step": context.get("step"),
            "step_total": context.get("step_total"),
            "outside_workdir": outside,
            "in_plan": display in planned or raw in planned,
        }
        if first is None:
            first = entry
        target = journal_path(workdir)
        if target is not None:
            _append_entry(target, entry)
        try:
            from src.core.messaging.event_types import RepoChangeRecorded

            bus = getattr(orch, "event_bus", None)
            if bus is not None:
                bus.publish_typed(
                    RepoChangeRecorded(
                        path=display,
                        tool=str(tool_name),
                        change=kind,
                        reason=str(entry["reason"]),
                        bytes_after=after[1],
                        workdir=str(workdir),
                    )
                )
        except Exception as exc:
            logger.debug("change_log: publish skipped: %s", exc)
    return first


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(entries: List[Mapping[str, Any]]) -> str:
    """Render entries as a "what changed and why" report.

    One line per distinct file, newest write last, with the tool that made the
    most recent change and the plan context that justified it.  Returns ``""``
    for an empty trail (and for one with no usable rows).
    """
    if not entries:
        return ""
    order: List[str] = []
    latest: Dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        path = str(entry.get("path") or "")
        if not path:
            continue
        if path not in latest:
            order.append(path)
        latest[path] = entry
    lines = ["Files changed:"]
    if not order:
        return ""
    for path in order:
        entry = latest[path]
        change = str(entry.get("change") or UNCHANGED)
        tool = str(entry.get("tool") or "?")
        reason = str(entry.get("reason") or "unattributed")
        lines.append(f"  [{change}] {path}  via {tool}  — {reason}")
    return "\n".join(lines)
