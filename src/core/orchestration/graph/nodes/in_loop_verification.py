"""in_loop_verification.py — P3.1 per-step verification inside the frontier loop.

The production frontier/lite graphs execute tools exclusively in
``frontier_loop_node``.  ``verification_node`` only runs *after* the loop exits,
so a model that writes a file with a syntax error, or reports a successful
``delete_file`` that left the file on disk, used to be told about it a turn or
two later — or never, if the loop exited on ``no tool calls``.

This module is the in-loop checkpoint: immediately after a modifying tool call
succeeds, run a cheap deterministic check on what changed and hand the failure
back to the model inside the same loop, so it can self-correct before building
further work on top of a broken file.

Design constraints:

* **Cheap and bounded.** Reuses ``lint_dispatch.quick_lint`` (the same fast
  per-file syntax check ``write_file``/``edit_file`` already use) and only for
  parse-only extensions.  ``.ts``/``.tsx``/``.rs`` are excluded because
  ``quick_lint`` shells out to a full type-checker/compiler for those: the
  diagnostics are dominated by pre-existing project-wide issues and each call
  costs seconds, which is wrong for a per-write checkpoint.
* **No new failures on pre-existing breakage.** Every path is linted *before*
  the tool runs and the normalised key set is reused, so only errors
  *introduced* by the write are reported — the same baseline-diff contract as
  ``_lint_verify.verify_candidate_content`` (which validates the candidate
  *before* the write; this module is the belt-and-suspenders *after* it, and
  the only checkpoint for tools with no pre-write coverage).
* **Never raises, never blocks.** Every external call is guarded; a check that
  cannot run returns ``None`` and the loop proceeds unchanged.  Failure
  signalling is data, not an exception.
* **Canonical tool names only.** ``resolve_tool_alias`` is applied first so an
  alias (``write`` -> ``write_file``) cannot evade the ``MODIFYING_TOOLS``
  membership test, mirroring the pipeline's single normalization point.

Public surface:

* :func:`capture_lint_baseline` — pre-tool lint snapshot (or ``None``).
* :func:`run_inline_check` — post-tool check returning a verification record.
* :func:`build_inline_verification_message` — record -> chat message.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Parse-only extensions where ``quick_lint`` is a fast, local syntax check.
#: Deliberately excludes the type-checker/compiler-backed ones (tsc, rustc).
INLINE_CHECK_EXTS: frozenset[str] = frozenset(
    {".py", ".js", ".mjs", ".cjs", ".jsx", ".go"}
)

#: Argument keys that carry a file path, in preference order.  ``new_path`` is
#: the destination of ``rename_file``; both it and ``path`` are considered.
_PATH_KEYS: tuple[str, ...] = ("path", "file_path", "filepath", "new_path", "target")

#: Upper bound on paths checked per tool call (renames yield two).
_MAX_PATHS: int = 3

#: Cap on error lines rendered per path so a broken file cannot flood history.
_MAX_ERRORS_PER_PATH: int = 5

#: Sentinel returned for "not applicable" baselines.
_NOT_APPLICABLE = None


def _canonical_tool(tool_name: Any) -> str:
    """Resolve a tool name (and its alias) to the canonical name."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return ""
    name = tool_name.strip()
    try:
        from src.tools.tools_config import resolve_tool_alias

        resolved = resolve_tool_alias(name)
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()
    except Exception:
        pass
    return name


def _is_modifying(canonical: str) -> bool:
    """True when *canonical* is a file-modifying tool.

    Membership comes from the canonical ``src.tools.constants`` set (aliases
    already resolved), never from a private copy.
    """
    if not canonical:
        return False
    try:
        from src.tools.constants import MODIFYING_TOOLS

        return canonical in MODIFYING_TOOLS
    except Exception:
        return False


def _extract_paths(tool_args: Any) -> List[str]:
    """Return the de-duplicated, non-empty path arguments of a tool call."""
    if not isinstance(tool_args, dict):
        return []
    out: List[str] = []
    for key in _PATH_KEYS:
        value = tool_args.get(key)
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if candidate not in out:
                out.append(candidate)
        if len(out) >= _MAX_PATHS:
            break
    return out


def _checkable_paths(tool_args: Any) -> List[str]:
    return [p for p in _extract_paths(tool_args) if Path(p).suffix.lower() in INLINE_CHECK_EXTS]


def _resolve(path: str, workdir: Any) -> str:
    """Resolve a tool path argument against the working directory."""
    try:
        base = Path(str(workdir)) if workdir else Path.cwd()
    except Exception:
        base = Path.cwd()
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    return str(base / candidate)


def _path_tokens(path: str) -> List[str]:
    """Path spellings the linter may echo back in a message."""
    tokens = [path]
    try:
        tokens.append(Path(path).name)
    except Exception:
        pass
    return [t for t in tokens if t]


def _lint_errors(path: str, workdir: Any) -> List[Dict[str, Any]]:
    """Run the fast linter on *path*; return its error dicts (never raises)."""
    try:
        from src.tools.lint_dispatch import quick_lint

        result = quick_lint(path, workdir if isinstance(workdir, Path) else Path(str(workdir or ".")))
    except Exception as exc:  # pragma: no cover - linter must never break the loop
        logger.debug("in_loop_verification: quick_lint(%s) unavailable: %s", path, exc)
        return []
    errors = (result or {}).get("lint_errors") or []
    return [e for e in errors if isinstance(e, dict)]


def _baseline_keys(path: str, workdir: Any) -> set:
    """Normalised key set of the lint errors a file already has."""
    try:
        from src.tools._lint_verify import _norm_lint_key
    except Exception:  # pragma: no cover - defensive
        return set()
    return {_norm_lint_key(err, _path_tokens(path)) for err in _lint_errors(path, workdir)}


def _succeeded(tool_result: Any) -> bool:
    """Conservative success probe over the several tool-result shapes."""
    if not isinstance(tool_result, dict):
        return False
    if tool_result.get("ok") is False or tool_result.get("success") is False:
        return False
    if str(tool_result.get("status") or "").lower() in ("error", "failed", "failure"):
        return False
    inner = tool_result.get("result")
    if isinstance(inner, dict):
        if inner.get("ok") is False or inner.get("success") is False:
            return False
        if str(inner.get("status") or "").lower() in ("error", "failed", "failure"):
            return False
    return True


def capture_lint_baseline(
    tool_name: Any,
    tool_args: Any,
    workdir: Any,
    baselines: Optional[Dict[str, set]] = None,
) -> Optional[Dict[str, set]]:
    """Snapshot the lint errors of a tool call's target paths *before* it runs.

    Returns a mapping of requested path -> baseline error key set, or ``None``
    when the checkpoint does not apply (non-modifying tool, unknown shape, no
    checkable path).  Never raises.

    ``baselines`` is an optional per-invocation cache.  A path is linted only
    the *first* time it is touched in an invocation and the result is reused
    for every later write to it.  That makes the checkpoint sticky in the right
    direction: a file the agent broke is re-reported on each subsequent write
    until it is actually repaired (the model must be told again, or it will
    never learn), while errors that predate the invocation stay unblamed
    forever.  Per-call re-capture would do the opposite — silence the model on
    the second identical bad write.
    """
    if not _is_modifying(_canonical_tool(tool_name)):
        return _NOT_APPLICABLE
    paths = _checkable_paths(tool_args)
    if not paths:
        return _NOT_APPLICABLE
    try:
        store = baselines if isinstance(baselines, dict) else {}
        resolved: Dict[str, set] = {}
        for path in paths:
            if path not in store:
                store[path] = _baseline_keys(_resolve(path, workdir), workdir)
            resolved[path] = store[path]
        return resolved
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("in_loop_verification: baseline capture failed: %s", exc)
        return _NOT_APPLICABLE


def _check_deletion(tool_name: str, tool_args: Any, workdir: Any) -> Optional[Dict[str, Any]]:
    """Assert that a successful ``delete_file`` actually removed the file."""
    paths = _extract_paths(tool_args)
    if not paths:
        return None
    target = _resolve(paths[0], workdir)
    try:
        still_there = Path(target).exists()
    except Exception:
        return None
    return {
        "tool": tool_name,
        "status": "failed" if still_there else "passed",
        "checked": 1,
        "failures": (
            [
                {
                    "path": paths[0],
                    "errors": [
                        "file still exists after a successful delete_file call"
                    ],
                }
            ]
            if still_there
            else []
        ),
    }


def _check_syntax(
    tool_name: str,
    tool_args: Any,
    workdir: Any,
    baseline: Optional[Dict[str, set]],
) -> Optional[Dict[str, Any]]:
    """Diff each target path's post-write lint errors against its baseline."""
    paths = _checkable_paths(tool_args)
    if not paths:
        return None
    baseline = baseline or {}
    failures: List[Dict[str, Any]] = []
    checked = 0
    for requested in paths:
        resolved = _resolve(requested, workdir)
        try:
            if not Path(resolved).exists():
                # A rename/move legitimately removes the old path; nothing to
                # lint and nothing to report.
                continue
        except Exception:
            continue
        checked += 1
        before = baseline.get(requested) or set()
        try:
            from src.tools._lint_verify import _norm_lint_key
        except Exception:  # pragma: no cover - defensive
            continue
        new_errors: List[str] = []
        tokens = _path_tokens(resolved)
        for err in _lint_errors(resolved, workdir):
            if _norm_lint_key(err, tokens) in before:
                continue
            line = err.get("line")
            message = str(err.get("message") or "").strip()
            code = str(err.get("code") or "").strip()
            prefix = f"line {line}: " if line is not None else ""
            suffix = f" [{code}]" if code else ""
            new_errors.append(f"{prefix}{message}{suffix}".strip())
            if len(new_errors) >= _MAX_ERRORS_PER_PATH:
                break
        if new_errors:
            failures.append({"path": requested, "errors": new_errors})
    if not checked:
        return None
    return {
        "tool": tool_name,
        "status": "failed" if failures else "passed",
        "checked": checked,
        "failures": failures,
    }


def run_inline_check(
    tool_name: Any,
    tool_args: Any,
    tool_result: Any,
    workdir: Any,
    baseline: Optional[Dict[str, set]] = None,
) -> Optional[Dict[str, Any]]:
    """Verify what a *successful* tool call changed; return a record or ``None``.

    ``None`` means "nothing to say" — non-modifying tool, unsupported shape, no
    checkable path, or a tool call that did not report success.  A returned
    record always carries ``status`` (``"passed"``/``"failed"``), ``tool``,
    ``checked`` and ``failures``.  Never raises.
    """
    canonical = _canonical_tool(tool_name)
    if not _is_modifying(canonical):
        return None
    if not _succeeded(tool_result):
        return None
    try:
        if canonical == "delete_file":
            return _check_deletion(canonical, tool_args, workdir)
        return _check_syntax(canonical, tool_args, workdir, baseline)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("in_loop_verification: check failed for %r: %s", canonical, exc)
        return None


def build_inline_verification_message(record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Render a failed record as a chat message for the model to act on.

    A ``role="user"`` message is used deliberately: it survives
    ``_normalize_messages_for_native_tools`` as ordinary text (that function
    only rewrites ``{"tool_execution_result": ...}`` payloads into
    ``role="tool"``), so the checkpoint feedback reaches the model on every
    provider protocol, including native tool calling where a synthesised tool
    message would need a matching ``tool_call_id``.
    """
    if not isinstance(record, dict) or record.get("status") != "failed":
        return {}
    tool = record.get("tool") or "tool"
    lines = [
        f"[inline verification] The previous {tool} call left new problems behind."
    ]
    for failure in record.get("failures") or []:
        lines.append(f"- {failure.get('path') or '<unknown path>'}")
        for err in (failure.get("errors") or [])[:_MAX_ERRORS_PER_PATH]:
            lines.append(f"    {err}")
    lines.append("Fix these before continuing.")
    return {"role": "user", "content": "\n".join(lines)}
