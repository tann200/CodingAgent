"""Work summary generation for completed agent tasks.

Extracted from orchestrator.py (Phase G1) — single responsibility.
Functions here are module-level so they can be imported and tested in isolation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.orchestration.tool_result_formatter import _format_side_by_side_diff


def _is_git_repo(path: str) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=path,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except Exception:
        return False


def _get_git_diff_for_files(workdir: str, files: List[str]) -> str:
    """Get git diff stat for specific files only."""
    if not files:
        return ""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD", "--"] + files,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=workdir,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _generate_work_summary(
    final_state: Optional[Dict[str, Any]], history: List[Dict[str, Any]]
) -> str:
    """Generate a summary of work done based on final state and history."""
    if not final_state:
        return ""

    task = final_state.get("task", final_state.get("original_task", ""))
    rounds = final_state.get("rounds", 0)
    current_plan = final_state.get("current_plan") or []
    current_step = final_state.get("current_step", 0)
    verified_reads = final_state.get("verified_reads") or []

    tool_counts: Dict[str, int] = {}
    tool_errors: List[str] = []
    for entry in history:
        if entry.get("role") == "tool" and entry.get("tool"):
            tool_name = entry.get("tool", "unknown")
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            # Collect tool-level errors to surface in summary
            result = entry.get("result") or {}
            if isinstance(result, dict):
                err = result.get("error") or result.get("message")
                if err and result.get("status") == "error":
                    tool_errors.append(f"{tool_name}: {err}")

    # Determine overall outcome from last_result
    last_result = final_state.get("last_result") or {}
    last_ok = last_result.get("ok") or last_result.get("status") == "ok"
    no_plan_fail = int(final_state.get("no_plan_fail_count") or 0)
    task_failed = (not last_ok and last_result) or no_plan_fail >= 3

    completed_steps = []
    pending_steps = []
    if current_plan:
        for i, step in enumerate(current_plan):
            desc = step.get("description", f"Step {i + 1}")
            if step.get("completed"):
                completed_steps.append(desc)
            elif i >= current_step:
                pending_steps.append(desc)

    lines = ["", "---", "**Work Summary**", ""]
    lines.append(f"- Task: {task}")
    lines.append(f"- Rounds: {rounds}")

    # Outcome indicator
    if task_failed:
        lines.append("- Outcome: ✗ Failed")
    else:
        lines.append("- Outcome: ✓ Completed")

    if tool_counts:
        tools_str = ", ".join(
            f"{count}× {name}" for name, count in sorted(tool_counts.items())
        )
        lines.append(f"- Tools used: {tools_str}")

    if verified_reads:
        lines.append(f"- Files inspected: {len(verified_reads)}")

    if completed_steps:
        lines.append(f"- Steps completed: {len(completed_steps)}/{len(current_plan)}")
        for step in completed_steps:
            lines.append(f"  - {step}")

    if pending_steps:
        lines.append(f"- Pending steps: {len(pending_steps)}")

    # Surface tool errors so the user can see what went wrong
    if tool_errors:
        lines.append("- Errors:")
        for err in tool_errors[-3:]:  # cap at last 3 to keep summary readable
            lines.append(f"  - ✗ {err}")

    # Only show git diff if: git is available AND files were modified during this session
    working_dir = final_state.get("working_dir", ".")
    if _is_git_repo(working_dir):
        # Get files modified during this session
        modified_files = final_state.get("_session_modified_files") or []
        if modified_files:
            # Filter to files within working_dir and get relative paths
            try:
                workdir_path = Path(working_dir).resolve()
                relative_files = []
                for f in modified_files:
                    try:
                        fpath = Path(f).resolve()
                        if str(fpath).startswith(str(workdir_path)):
                            relative_files.append(str(fpath.relative_to(workdir_path)))
                    except Exception:
                        pass

                if relative_files:
                    # Get unified diff for side-by-side formatting
                    diff_result = subprocess.run(
                        [
                            "git",
                            "diff",
                            "--",
                        ]
                        + relative_files,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        cwd=working_dir,
                    )
                    if diff_result.returncode == 0 and diff_result.stdout.strip():
                        unified_diff = diff_result.stdout.strip()
                        side_by_side = _format_side_by_side_diff(unified_diff)

                        lines.append("")
                        lines.append("**📋 Changes Made:**")
                        lines.append("")
                        lines.append("```diff")
                        lines.append(side_by_side)
                        lines.append("```")
            except Exception:
                pass

    return "\n".join(lines)
