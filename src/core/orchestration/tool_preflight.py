"""Preflight validation for tool calls.

Extracted from orchestrator.py (Phase G4) — single responsibility.
The main entry point is ``preflight_check_impl(orch, tool_call)``.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any, Dict


def preflight_check_impl(orch: Any, tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a tool call before execution.

    Checks:
    1. Tool name is a string and is registered.
    2. Bash commands don't contain dangerous patterns (HR-5).
    3. File paths for write operations are inside working_dir.
    """
    name_raw = tool_call.get("name")
    if not isinstance(name_raw, str):
        return {"ok": False, "error": "Tool name must be a string."}
    name = name_raw
    args = tool_call.get("arguments", {})

    tool = orch.tool_registry.get(name)
    if not tool:
        # TOOLS-03: structured tool-repair response instead of bare error string.
        # Include a short list of the closest-sounding registered tool names so
        # the LLM can self-correct without a full round-trip.
        _all_tool_names = (
            sorted(orch.tool_registry.tools.keys())
            if hasattr(orch.tool_registry, "tools")
            else []
        )
        # P3-D: Auto-correct high-confidence typos (cutoff=0.85, single unique match)
        # for SMALL and above.  NANO models should fail fast (wrong name = context leak).
        _auto_corrected = False
        try:
            _model_tier = getattr(orch, "_model_tier", None) or ""
            _tier_lower = str(_model_tier).lower()
            if _tier_lower not in ("nano", ""):
                _high_conf = difflib.get_close_matches(
                    name, _all_tool_names, n=1, cutoff=0.85
                )
                if _high_conf:
                    _corrected_name = _high_conf[0]
                    _corrected_tool = orch.tool_registry.get(_corrected_name)
                    if _corrected_tool:
                        import logging as _log

                        _log.getLogger(__name__).warning(
                            "preflight: P3-D auto-corrected tool '%s' → '%s'",
                            name,
                            _corrected_name,
                        )
                        name = _corrected_name
                        tool_call["name"] = _corrected_name
                        tool = _corrected_tool
                        _auto_corrected = True
        except Exception:
            pass

        if not _auto_corrected:
            _closest = difflib.get_close_matches(name, _all_tool_names, n=5, cutoff=0.4)
            return {
                "ok": False,
                "error": "tool_not_found",
                "tool_name": name,
                "message": (
                    f"Tool '{name}' is not registered. "
                    + (
                        f"Did you mean one of: {', '.join(_closest)}?"
                        if _closest
                        else "Check the tool name spelling."
                    )
                ),
                "suggestions": _closest,
            }

    # HR-5 fix: validate bash commands at preflight time (defence-in-depth).
    # file_tools.bash() also checks these, but catching them here provides
    # an earlier rejection path and a consistent error source.
    if name == "bash":
        command = args.get("command") or args.get("cmd") or ""
        if command:
            cmd_normalised = re.sub(r"\s+", " ", str(command)).lower()
            for pattern in orch._BASH_DANGEROUS_PATTERNS:
                if pattern in cmd_normalised:
                    return {
                        "ok": False,
                        "error": (
                            f"Preflight: bash command contains dangerous pattern "
                            f"'{pattern}'. No shell operators or destructive commands allowed."
                        ),
                    }

    path_arg = args.get("path") or args.get("file_path")
    if path_arg and "write" in tool.get("side_effects", []):
        try:
            # Resolve the path and see if it's inside working_dir
            target_path = (Path(orch.working_dir or ".") / path_arg).resolve()
            work_dir = Path(orch.working_dir or ".").resolve()
            if not target_path.is_relative_to(work_dir):
                return {
                    "ok": False,
                    "error": f"Path '{path_arg}' is outside working directory.",
                }
        except Exception as e:
            return {"ok": False, "error": f"Invalid path: {e}"}

    return {"ok": True}
