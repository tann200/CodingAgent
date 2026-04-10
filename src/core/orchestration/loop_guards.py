"""
loop_guards.py — Centralised loop-prevention guards for the execution node.

Extracted from execution_node.py as per ORCH-02 in
docs/orchestration-gap-analysis.md.

Three guards are provided:

1. check_read_before_write(tool_name, path_arg, state, working_dir, session_read_files)
   Returns a non-None error dict when a modifying tool is called on an existing
   file that has not yet been read in this session.

2. check_cooldown(tool_name, args, state)
   Returns a non-None error dict when a read-only tool is called again before
   COOLDOWN_GAP other tool calls have occurred.  Prevents token-wasting
   re-fetches of content already in context.

3. check_doom_loop(tool_name, args, state, event_bus=None)
   Returns a non-None error dict when the last DOOM_LOOP_THRESHOLD tool calls
   all share the same (name, args) fingerprint.  Consults
   PermissionPolicy.check_doom_loop() (PERM-02) so the user can override the
   default DENY behaviour via ~/.coding_agent/permissions.json.

All three guards are pure functions — they never mutate state or produce side
effects other than logging.  The execution node is responsible for updating
AgentState from the returned dicts.

Usage
-----
    from src.core.orchestration.loop_guards import (
        check_read_before_write,
        check_cooldown,
        check_doom_loop,
    )

    err = check_read_before_write(tool_name, path_arg, state, working_dir, session_read_files)
    if err:
        return err

    err = check_cooldown(tool_name, args, state)
    if err:
        return err

    err, updated_recent = check_doom_loop(tool_name, args, state, event_bus)
    if err:
        return {**err, "recent_tool_calls": updated_recent}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (single source of truth — no more magic numbers scattered across
# execution_node.py and orchestrator.py)
# ---------------------------------------------------------------------------

#: Tools that require a prior successful read_file call on the target path.
MODIFYING_TOOLS: Set[str] = {
    "edit_file",
    "edit_file_atomic",  # SEC-2: sync with WRITE_TOOLS_REQUIRING_READ in orchestrator
    "edit_by_line_range",
    "apply_patch",
    "write_file",
    "delete_file",
    "rename_file",  # SEC-2: sync with WRITE_TOOLS_REQUIRING_READ in orchestrator
    "ast_rename",  # SEC-2: sync with WRITE_TOOLS_REQUIRING_READ in orchestrator
    "manage_todo",  # writes TODO.md; enforce read-before-write (TS-5)
}

#: Read-only tools subject to the cooldown gate.
COOLDOWN_READ_TOOLS: Set[str] = {
    "read_file",
    "fs.read",
    "grep",
    "search_code",
    "find_symbol",
    "glob",
}

#: Minimum number of *other* tool calls required between two identical
#: cooldown-key invocations.  Mirrors OpenCode's pattern.
COOLDOWN_GAP: int = 3

#: Number of consecutive identical fingerprints that triggers doom-loop
#: detection.  OpenCode uses the same value.
DOOM_LOOP_THRESHOLD: int = 3

#: Maximum length of the recent_tool_calls ring buffer.
RECENT_CALLS_WINDOW: int = 10

#: Window size for alternating-loop detection.  If the last N tool calls
#: involve ≤ ALTERNATING_TOOL_DIVERSITY distinct tools, an alternating loop
#: is suspected (e.g. read_file ↔ edit_file with the same target forever).
ALTERNATING_LOOP_WINDOW: int = 10
ALTERNATING_TOOL_DIVERSITY: int = 2


# ---------------------------------------------------------------------------
# Guard 1 — Read-before-write
# ---------------------------------------------------------------------------


def check_read_before_write(
    tool_name: str,
    path_arg: Optional[str],
    state: Mapping[str, Any],
    working_dir: str,
    session_read_files: Optional[Set[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Return an error result dict if *tool_name* violates read-before-write.

    Returns None when the guard passes (execution should continue).

    Parameters
    ----------
    tool_name:
        Name of the tool being executed.
    path_arg:
        The ``path`` or ``file_path`` argument extracted from the tool call.
    state:
        Current AgentState dict.
    working_dir:
        Absolute path to the session working directory.
    session_read_files:
        Optional set of resolved paths already read this session (from
        ``orchestrator._session_read_files``).  Augments the state-based
        ``files_read`` and ``verified_reads`` lookups.
    """
    if tool_name not in MODIFYING_TOOLS or not path_arg:
        return None

    try:
        resolved = str((Path(working_dir) / path_arg).resolve())
        if not Path(resolved).exists():
            # New files don't need a prior read.
            return None

        files_read_map: Dict[str, Any] = state.get("files_read") or {}
        verified_reads: Set[str] = set(state.get("verified_reads") or [])
        _session = session_read_files or set()

        if (
            resolved not in files_read_map
            and resolved not in verified_reads
            and resolved not in _session
        ):
            err_msg = (
                f"Security/Logic violation: You must read '{path_arg}' "
                f"before writing to it. Use read_file first to inspect "
                f"the current content."
            )
            return {
                "last_result": {"ok": False, "error": err_msg},
                "history": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"tool_execution_result": {"ok": False, "error": err_msg}}
                        ),
                    }
                ],
                "next_action": None,
            }
    except Exception as exc:
        logger.debug(
            "loop_guards.check_read_before_write: exception (non-fatal): %s", exc
        )

    return None


# ---------------------------------------------------------------------------
# Guard 2 — Cooldown
# ---------------------------------------------------------------------------


def check_cooldown(
    tool_name: str,
    args: Dict[str, Any],
    state: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return an error result dict if *tool_name* is in its cooldown window.

    Returns None when the guard passes (execution should continue).
    """
    if tool_name not in COOLDOWN_READ_TOOLS:
        return None

    path_arg = args.get("path") or args.get("file_path")
    primary_arg = (
        args.get("name")  # find_symbol
        or args.get("query")  # search_code
        or args.get("pattern")  # grep
        or path_arg  # read_file, glob, fs.read
        or ""
    )
    cooldown_key = f"{tool_name}:{primary_arg}"
    tool_last_used: Dict[str, int] = state.get("tool_last_used") or {}
    current_count: int = int(state.get("tool_call_count") or 0)
    last_count: int = tool_last_used.get(cooldown_key, current_count - COOLDOWN_GAP - 1)

    if current_count - last_count < COOLDOWN_GAP:
        cooldown_msg = (
            f"Tool '{tool_name}'"
            + (f" on '{path_arg}'" if path_arg else "")
            + f" was called {current_count - last_count} execution(s) ago. "
            "The result is already in context — please use the existing context "
            "instead of re-fetching. Try a different approach or proceed with what you have."
        )
        logger.warning(
            "loop_guards.check_cooldown: %s (%d < %d)",
            cooldown_key,
            current_count - last_count,
            COOLDOWN_GAP,
        )
        return {
            "last_result": {"ok": False, "error": cooldown_msg},
            "history": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {"tool_execution_result": {"ok": False, "error": cooldown_msg}}
                    ),
                }
            ],
            "next_action": None,
            "tool_call_count": current_count + 1,  # count the blocked attempt
        }

    return None


# ---------------------------------------------------------------------------
# Guard 3 — Doom-loop detection (ORCH-01 + PERM-02)
# ---------------------------------------------------------------------------


def _canonical_fingerprint(tool_name: str, args: Dict[str, Any]) -> str:
    """Return a pagination-agnostic fingerprint for *tool_name* + *args*.

    P2-D: Strips ephemeral pagination arguments (``offset``, ``start_line``,
    ``end_line``, ``page``) before serialising so that a read-then-paginate
    sequence is recognised as the same repeated action rather than escaping
    doom-loop detection because the offset counter increments each turn.

    All other arguments are sorted and serialised identically to the original
    fingerprint, so callers that don't paginate are unaffected.
    """
    _PAGINATION_KEYS = frozenset({"offset", "start_line", "end_line", "page", "limit"})
    canonical_args = {k: v for k, v in args.items() if k not in _PAGINATION_KEYS}
    return f"{tool_name}:{json.dumps(canonical_args, sort_keys=True, default=str)}"


def _detect_alternating_loop(
    recent: List[str],
    new_fingerprint: str,
) -> bool:
    """Return True when the last ALTERNATING_LOOP_WINDOW calls use ≤ ALTERNATING_TOOL_DIVERSITY distinct tool names.

    P2-D: Catches alternating-tool loops (e.g. read_file → edit_file →
    read_file → edit_file …) that escape the identical-fingerprint doom-loop
    check because the args differ slightly each turn.
    """
    window = (recent + [new_fingerprint])[-ALTERNATING_LOOP_WINDOW:]
    if len(window) < ALTERNATING_LOOP_WINDOW:
        # Not enough history yet.
        return False
    distinct_tools: set = set()
    for fp in window:
        # Tool name is the prefix before the first colon.
        tool = fp.split(":", 1)[0]
        distinct_tools.add(tool)
        if len(distinct_tools) > ALTERNATING_TOOL_DIVERSITY:
            return False
    return True


def check_doom_loop(
    tool_name: str,
    args: Dict[str, Any],
    state: Mapping[str, Any],
    event_bus: Optional[Any] = None,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Check for a doom loop and return *(error_dict_or_None, updated_recent_calls)*.

    The second element of the tuple is the updated ``recent_tool_calls`` list
    with the current fingerprint appended (always returned so the caller can
    persist it regardless of whether a loop was detected).

    Two loop patterns are detected:

    1. **Identical-call doom loop**: the last DOOM_LOOP_THRESHOLD canonical
       fingerprints are identical (pagination offsets stripped — P2-D).
    2. **Alternating-tool loop**: the last ALTERNATING_LOOP_WINDOW calls use
       ≤ ALTERNATING_TOOL_DIVERSITY distinct tool names (P2-D).

    When a doom loop IS detected:
    - Consults ``PermissionPolicy.check_doom_loop()`` (PERM-02).
    - DENY (default) → returns a hard error result.
    - ASK → publishes ``tool.doom_loop_detected`` on *event_bus* (if provided)
      and returns the same hard error with an ``"ask"`` marker so the TUI can
      surface an interactive approval dialog.
    - ALLOW → the user has explicitly opted in; returns (None, updated_recent).

    Parameters
    ----------
    tool_name:
        Name of the tool being executed.
    args:
        Argument dict for the tool call.
    state:
        Current AgentState dict.
    event_bus:
        Optional EventBus instance.  When provided and doom-loop behavior is
        ASK, a ``tool.doom_loop_detected`` event is published.
    """
    # P2-D: use pagination-agnostic canonical fingerprint for the ring buffer.
    fingerprint = _canonical_fingerprint(tool_name, args)
    recent: List[str] = list(state.get("recent_tool_calls") or [])
    tail = recent[-(DOOM_LOOP_THRESHOLD - 1) :]

    # Append BEFORE returning so the ring buffer stays accurate in all paths.
    updated_recent = (recent + [fingerprint])[-RECENT_CALLS_WINDOW:]

    # Pattern 1: consecutive identical fingerprints.
    identical_doom = len(tail) >= DOOM_LOOP_THRESHOLD - 1 and all(
        fp == fingerprint for fp in tail
    )

    # Pattern 2: alternating-tool loop (P2-D).
    alternating_doom = _detect_alternating_loop(recent, fingerprint)

    doom_detected = identical_doom or alternating_doom
    doom_kind = (
        "alternating" if (alternating_doom and not identical_doom) else "identical"
    )

    if not doom_detected:
        return None, updated_recent

    # --- Doom loop confirmed ---
    logger.warning(
        "loop_guards.check_doom_loop: doom loop detected for '%s' (kind=%s)",
        tool_name,
        doom_kind,
    )

    # PERM-02: consult PermissionPolicy
    behavior_str = "deny"  # safe default if policy import fails
    try:
        from src.core.orchestration.permission_policy import (
            Behavior,
            get_permission_policy,
        )

        policy = get_permission_policy()
        doom_behavior = policy.check_doom_loop()
        behavior_str = doom_behavior.value

        if doom_behavior == Behavior.ALLOW:
            # User explicitly opted in via permissions.json — let the call through.
            logger.info(
                "loop_guards.check_doom_loop: policy=ALLOW; allowing repeated call"
            )
            return None, updated_recent

        if doom_behavior == Behavior.ASK and event_bus is not None:
            try:
                event_bus.publish(
                    "tool.doom_loop_detected",
                    {
                        "tool": tool_name,
                        "fingerprint": fingerprint,
                        "behavior": "ask",
                    },
                )
            except Exception as pub_exc:
                logger.debug(
                    "loop_guards.check_doom_loop: event_bus publish failed: %s", pub_exc
                )

    except Exception as policy_exc:
        logger.debug(
            "loop_guards.check_doom_loop: policy lookup failed (non-fatal): %s",
            policy_exc,
        )

    loop_msg: str
    if doom_kind == "alternating":
        distinct_in_window = len({fp.split(":", 1)[0] for fp in updated_recent})
        loop_msg = (
            f"[ALTERNATING LOOP] The last {ALTERNATING_LOOP_WINDOW} tool calls "
            f"used only {distinct_in_window} distinct tool(s). This indicates a "
            "stuck alternating loop (e.g. read → edit → read → edit …). "
            "Try a completely different approach, inspect the current state "
            "holistically, or report that the task cannot be completed as specified."
        )
    else:
        loop_msg = (
            f"[DOOM LOOP] Tool '{tool_name}' called {DOOM_LOOP_THRESHOLD} times "
            "consecutively with identical arguments. This indicates a stuck loop. "
            "Try a different approach — read the current state, use a different tool, "
            "or report that the task cannot be completed as specified."
        )

    error_result: Dict[str, Any] = {
        "last_result": {"ok": False, "error": loop_msg},
        "history": [
            {
                "role": "user",
                "content": json.dumps(
                    {"tool_execution_result": {"ok": False, "error": loop_msg}}
                ),
            }
        ],
        "next_action": None,
        "doom_loop_behavior": behavior_str,  # exposed for TUI / tests
    }

    return error_result, updated_recent
