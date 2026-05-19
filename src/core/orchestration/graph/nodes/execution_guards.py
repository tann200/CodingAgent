"""execution_guards.py — P3-T6: Guard/validation helpers extracted from execution_helpers.

Contains:
- _validate_python_syntax: syntax check before writing .py files
- _capture_snapshot: pre-write snapshot capture for rollback
- check_agent_definition_tool_gate: P3-1 AgentDefinition allowed/denied gate
"""
from __future__ import annotations

import ast as _ast
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

_logger = logging.getLogger(__name__)


def _validate_python_syntax(content: str, path_hint: str = "") -> Optional[str]:
    """Return an error string if *content* is not valid Python, else None.

    Only applies to .py files. Non-Python content always returns None.
    Called before committing a write_file result to prevent the agent from
    writing files that would immediately cause import/syntax errors.
    """
    if not path_hint.endswith(".py"):
        return None
    try:
        _ast.parse(content)
        return None
    except SyntaxError as exc:
        return (
            f"Syntax error in generated Python for '{path_hint}': "
            f"{exc.msg} (line {exc.lineno})"
        )


def _capture_snapshot(path: str, working_dir: str) -> Optional[str]:
    """Read the current content of *path* and save it to the snapshot dir.

    Must be called **before** a write_file / edit_file tool result is committed
    so that the pre-write content is preserved for rollback by debug_node.

    Returns the absolute snapshot file path on success, ``None`` on any error
    (non-fatal — snapshot loss is acceptable over crashing execution).
    """
    try:
        p = (Path(working_dir) / path).resolve()
        if not p.exists():
            return None
        snap_dir = Path(working_dir) / ".codingAgent" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        slug = hashlib.md5(str(p).encode()).hexdigest()[:8]
        snap_path = snap_dir / f"{slug}_{ts}{p.suffix}"
        snap_path.write_bytes(p.read_bytes())
        return str(snap_path)
    except Exception:
        return None


def check_agent_definition_tool_gate(
    *,
    orchestrator: Any,
    tool_name: str,
    logger: Any = None,
) -> Optional[Dict[str, Any]]:
    """P3-1: Check AgentDefinition.is_tool_permitted() for the active agent.

    Returns an error state dict if the tool is denied by the active agent's
    ``allowed_tools`` / ``denied_tools`` policy, else ``None``.

    This is a defence-in-depth gate that runs *after* the role_config gate in
    ``handle_execution_preflight_and_role_gate``.  It catches roles whose
    restrictions are defined only in ``agent_types.py`` (e.g. ``explore``,
    ``verification``) and ensures AgentDefinition policy is always enforced
    regardless of whether role_config has a matching entry.
    """
    _log = logger or _logger
    if orchestrator is None:
        return None
    _active_agent = getattr(orchestrator, "active_agent", None)
    if _active_agent is None:
        return None
    try:
        if not _active_agent.is_tool_permitted(tool_name):
            _agent_id = getattr(_active_agent, "id", "unknown")
            _err = (
                f"Tool '{tool_name}' is not permitted for agent '{_agent_id}' "
                "(denied by AgentDefinition policy)"
            )
            _log.warning("execution_node: %s", _err)
            return {
                "last_result": {"ok": False, "error": _err},
                "history": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"tool_execution_result": {"ok": False, "error": _err}}
                        ),
                    }
                ],
                "next_action": None,
            }
    except Exception as exc:
        _log.debug("execution_node: AgentDefinition gate error: %s", exc)
    return None
