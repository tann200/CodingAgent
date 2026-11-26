"""Headless NDJSON stream emitter and ``--parallel`` git worktree lifecycle (TASK-36).

- ``StreamJsonEmitter`` subscribes to ``Orchestrator.event_bus`` and emits live
  NDJSON lifecycle events (``turn_start``, ``tool_start``, ``tool_end``,
  ``verification``, ``retry``, ``compaction``, ``turn_end``) while strictly
  omitting raw secrets and raw tool bodies. Terminates with an authoritative
  ``{"type": "result", "status": "complete"|"failed"|"aborted", "result": ..., "usage": ...}``
  envelope whose exit code is ``0`` iff ``status == "complete"``.
- ``ParallelWorktreeSession`` implements ``--parallel``: creates a timestamped
  temporary git worktree and, on exit, attempts a local squash-merge back onto
  the base branch (never auto-pushes), prompting ``[a]bort / [l]eave / [h]elp``
  on merge conflict.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

from src.core.security.redaction import redact_secrets


def classify_headless_status(result: Dict[str, Any]) -> str:
    """Classify an orchestrator result dict into ``complete|failed|aborted``."""
    if not isinstance(result, dict):
        return "failed"
    if result.get("cancelled") or result.get("aborted") or result.get("interrupted"):
        return "aborted"
    status = str(result.get("status") or "").strip().lower()
    if status in ("cancelled", "aborted", "interrupted"):
        return "aborted"
    if result.get("error") or status in ("error", "failed", "incomplete"):
        return "failed"
    if result.get("ok") is False:
        return "failed"
    if status in ("complete", "completed", "ok", "success", ""):
        return "complete"
    return "complete"


def exit_code_for_status(status: str) -> int:
    """Return exit code ``0`` strictly on ``complete``; non-zero otherwise."""
    if status == "complete":
        return 0
    if status == "aborted":
        return 130
    return 1


class StreamJsonEmitter:
    """Emit sanitized NDJSON lifecycle events for ``--output stream-json``."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self._stream = stream or sys.stdout
        self._subscriptions: List[tuple[Any, str, Callable[[Any], None]]] = []
        self._turn = 0

    def emit(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "type": event_type,
            "ts": round(time.time(), 3),
        }
        for k, v in fields.items():
            if isinstance(v, str):
                record[k] = redact_secrets(v)
            else:
                record[k] = v
        line = json.dumps(record, ensure_ascii=False, default=str)
        self._stream.write(line + "\n")
        self._stream.flush()
        return record

    def attach(self, event_bus: Any) -> None:
        """Subscribe to lifecycle events on *event_bus* (never emitting raw tool bodies)."""
        if event_bus is None or not hasattr(event_bus, "subscribe"):
            return

        def _sub(name: str, fn: Callable[[Any], None]) -> None:
            event_bus.subscribe(name, fn)
            self._subscriptions.append((event_bus, name, fn))

        def _on_step_start(payload: Any) -> None:
            self._turn += 1
            step = payload.get("step", self._turn) if isinstance(payload, dict) else self._turn
            self.emit("turn_start", turn=step)

        def _on_step_finish(payload: Any) -> None:
            step = payload.get("step", self._turn) if isinstance(payload, dict) else self._turn
            self.emit("turn_end", turn=step)

        def _on_tool_start(payload: Any) -> None:
            tool = payload.get("tool", payload.get("tool_name", "unknown")) if isinstance(payload, dict) else "unknown"
            self.emit("tool_start", tool=str(tool))

        def _on_tool_finish(payload: Any) -> None:
            if isinstance(payload, dict):
                tool = str(payload.get("tool", payload.get("tool_name", "unknown")))
                ok = bool(payload.get("ok", True))
                self.emit("tool_end", tool=tool, ok=ok)
            else:
                self.emit("tool_end", tool="unknown", ok=True)

        def _on_tool_error(payload: Any) -> None:
            tool = str(payload.get("tool", "unknown")) if isinstance(payload, dict) else "unknown"
            self.emit("tool_end", tool=tool, ok=False)

        def _on_retry(payload: Any) -> None:
            attempt = payload.get("attempt_number", 1) if isinstance(payload, dict) else 1
            reason = str(payload.get("error_type", "")) if isinstance(payload, dict) else ""
            self.emit("retry", attempt=attempt, reason=reason)

        def _on_compaction(payload: Any) -> None:
            dropped = payload.get("dropped_count", 0) if isinstance(payload, dict) else 0
            self.emit("compaction", dropped_count=dropped)

        def _on_verification(payload: Any) -> None:
            passed = bool(payload.get("passed", payload.get("ok", False))) if isinstance(payload, dict) else False
            self.emit("verification", passed=passed)

        _sub("step.start", _on_step_start)
        _sub("step.finish", _on_step_finish)
        _sub("tool.execute.start", _on_tool_start)
        _sub("tool.execute.finish", _on_tool_finish)
        _sub("tool.execute.error", _on_tool_error)
        _sub("retry.attempt", _on_retry)
        _sub("message.compaction_applied", _on_compaction)
        _sub("context.compacted", _on_compaction)
        _sub("verification.completed", _on_verification)

    def detach(self) -> None:
        for bus, name, fn in self._subscriptions:
            try:
                bus.unsubscribe(name, fn)
            except Exception:
                pass
        self._subscriptions.clear()

    def emit_final_result(
        self,
        *,
        status: str,
        result_text: str,
        usage: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": status,
            "result": redact_secrets(str(result_text or "")),
            "usage": dict(usage or {}),
        }
        if error:
            payload["error"] = redact_secrets(str(error))
        return self.emit("result", **payload)


def _run_git(cwd: Path, args: List[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


class ParallelWorktreeSession:
    """Manage a timestamped temporary git worktree for ``--parallel`` (TASK-36).

    Never auto-pushes. On exit, attempts a local squash-merge back onto the
    original branch and prompts ``[a]bort / [l]eave / [h]elp`` on conflict.
    """

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.worktree_path: Optional[Path] = None
        self.branch_name: str = ""
        self.base_branch: str = ""

    def create(self) -> Path:
        head_res = _run_git(self.repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
        if head_res.returncode != 0:
            raise RuntimeError(f"--parallel requires a git repository: {head_res.stderr.strip()}")
        self.base_branch = head_res.stdout.strip() or "HEAD"
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = uuid.uuid4().hex[:6]
        self.branch_name = f"codingagent-parallel-{stamp}-{suffix}"
        wt_parent = Path(tempfile.mkdtemp(prefix="codingagent_parallel_"))
        wt_dir = wt_parent / self.branch_name
        res = _run_git(self.repo_root, ["worktree", "add", "-b", self.branch_name, str(wt_dir), "HEAD"])
        if res.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {res.stderr.strip()}")
        self.worktree_path = wt_dir
        return wt_dir

    def _cleanup_worktree(self) -> None:
        if self.worktree_path is not None:
            _run_git(self.repo_root, ["worktree", "remove", "--force", str(self.worktree_path)])
            if self.branch_name:
                _run_git(self.repo_root, ["branch", "-D", self.branch_name])
            self.worktree_path = None

    def finalize(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        print_fn: Callable[[str], None] = print,
    ) -> Dict[str, Any]:
        """Squash-merge changes back locally (never pushing); prompt on conflict."""
        if self.worktree_path is None:
            return {"status": "noop"}

        # Stage & commit any dirty changes inside the temp worktree so squash-merge sees them
        st = _run_git(self.worktree_path, ["status", "--porcelain"])
        if st.stdout.strip():
            _run_git(self.worktree_path, ["add", "-A"])
            _run_git(
                self.worktree_path,
                [
                    "-c",
                    "user.name=CodingAgent",
                    "-c",
                    "user.email=codingagent@local",
                    "commit",
                    "-m",
                    f"codingagent parallel session {self.branch_name}",
                ],
            )

        diff_check = _run_git(self.repo_root, ["rev-list", "--count", f"HEAD..{self.branch_name}"])
        if diff_check.returncode == 0 and diff_check.stdout.strip() == "0":
            self._cleanup_worktree()
            return {"status": "clean_no_changes"}

        merge = _run_git(self.repo_root, ["merge", "--squash", self.branch_name])
        if merge.returncode == 0:
            self._cleanup_worktree()
            print_fn(
                f"[parallel] Squash-merged changes from {self.branch_name} into working tree "
                "(staged, not pushed)."
            )
            return {"status": "merged", "branch": self.branch_name}

        # Conflict! Offer interactive [a]bort / [l]eave / [h]elp
        print_fn(f"[parallel] Squash-merge conflict from {self.branch_name}:")
        if merge.stdout.strip():
            print_fn(merge.stdout.strip())
        while True:
            try:
                choice = (input_fn("Resolve conflict: [a]bort / [l]eave / [h]elp > ") or "").strip().lower()
            except EOFError:
                choice = "l"
            if choice in ("h", "help"):
                print_fn(
                    "  [a]bort — reset the partial squash-merge in your repo and delete the temporary worktree.\n"
                    f"  [l]eave — reset the partial merge in your repo and keep worktree at {self.worktree_path} (branch {self.branch_name})."
                )
                continue
            if choice in ("a", "abort"):
                _run_git(self.repo_root, ["reset", "--merge"])
                self._cleanup_worktree()
                print_fn("[parallel] Aborted merge and removed temporary worktree.")
                return {"status": "aborted", "branch": self.branch_name}
            if choice in ("l", "leave"):
                _run_git(self.repo_root, ["reset", "--merge"])
                kept = str(self.worktree_path)
                print_fn(f"[parallel] Left worktree intact at {kept} (branch {self.branch_name}).")
                return {"status": "left", "worktree": kept, "branch": self.branch_name}
            print_fn("Please enter 'a' (abort), 'l' (leave), or 'h' (help).")


__all__ = [
    "ParallelWorktreeSession",
    "StreamJsonEmitter",
    "classify_headless_status",
    "exit_code_for_status",
]
