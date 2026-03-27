"""
TODO tracking tool for the coding agent.

Manages a human-readable TODO.md file at .agent-context/TODO.md so the user
can see task progress in real time and the agent can track which steps are done.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools._tool import tool
from src.tools.tools_config import agent_context_path

logger = logging.getLogger(__name__)

_TODO_FILENAME = "TODO.md"
_TODO_JSON_FILENAME = "todo.json"


def _todo_path(workdir: str) -> Path:
    return agent_context_path(Path(workdir)) / _TODO_FILENAME


def _todo_json_path(workdir: str) -> Path:
    return agent_context_path(Path(workdir)) / _TODO_JSON_FILENAME


def _load_todo_json(workdir: str) -> List[Dict[str, Any]]:
    p = _todo_json_path(workdir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return []


def _save_todo(workdir: str, steps: List[Dict[str, Any]]) -> None:
    base = agent_context_path(Path(workdir))

    # Save machine-readable JSON
    _todo_json_path(workdir).write_text(json.dumps(steps, indent=2))

    # Write human-readable Markdown with status and dependency annotations
    _STATUS_ICONS = {
        "pending":     "[ ]",
        "in_progress": "[~]",
        "blocked":     "[!]",
        "done":        "[x]",
        "verified":    "[✓]",
    }
    lines = ["# Agent TODO\n"]
    for i, step in enumerate(steps):
        status = step.get("status", "done" if step.get("done", False) else "pending")
        checkbox = _STATUS_ICONS.get(status, "[ ]")
        desc = step.get("description", f"Step {i + 1}")
        deps = step.get("depends_on", [])
        blocked_reason = step.get("blocked_reason")

        suffix_parts = []
        if deps:
            dep_names = [f"Step {d + 1}" for d in deps]
            suffix_parts.append(f"depends on: {', '.join(dep_names)}")
        if blocked_reason:
            suffix_parts.append(f"blocked: {blocked_reason}")
        suffix = f" *({', '.join(suffix_parts)})*" if suffix_parts else ""

        lines.append(f"- {checkbox} **Step {i + 1}:** {desc}{suffix}")

    _todo_path(workdir).write_text("\n".join(lines) + "\n")


@tool(side_effects=["write"], tags=["planning"])
def manage_todo(
    action: str,
    workdir: str,
    description: Optional[str] = None,
    step_id: Optional[int] = None,
    steps: Optional[List[str]] = None,
    depends_on: Optional[List[List[int]]] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Manage the agent TODO list.

    DEP extension: Added depends_on parameter for DAG support.

    Actions:
      - "create": Create a new TODO with optional dependencies.
                  Pass: steps=["Step 1", "Step 2"],
                        depends_on=[None, [0]] (Step 2 depends on Step 0)
      - "check":  Mark a step as completed.
      - "update": Update a step's description.
      - "read":   Return the current TODO list.
      - "clear":  Remove the TODO list.
      - "start":  Mark a step as in_progress (only one can be active at a time).
      - "block":  Mark a step as blocked with an optional reason.
      - "verify": Mark a step as verified (human/test confirmed).
      - "next":   Return the next executable step (all dependencies done).

    Args:
        action: One of "create", "check", "update", "read", "clear", "start", "block", "verify", "next"
        workdir: Working directory (used to locate .agent-context/)
        description: New description for update action, or block reason
        step_id: 0-based step index for check/update/start/block/verify actions
        steps: List of step description strings for create action
        depends_on: List of dependency lists for each step (for create action)
        reason: Block reason for block action

    Returns:
        Dict with status and the current TODO state
    """
    valid_actions = {
        "create",
        "check",
        "update",
        "read",
        "clear",
        "start",
        "block",
        "verify",
        "next",
    }
    if action not in valid_actions:
        return {
            "status": "error",
            "error": f"Unknown action '{action}'. Use: {', '.join(sorted(valid_actions))}",
        }

    try:
        if action == "create":
            if not steps:
                return {
                    "status": "error",
                    "error": "steps list is required for create action",
                }

            # Build step dicts with optional dependencies
            new_steps = []
            for i, s in enumerate(steps):
                step_dict = {"description": s, "done": False}
                if depends_on and i < len(depends_on) and depends_on[i] is not None:
                    step_dict["depends_on"] = depends_on[i]
                else:
                    step_dict["depends_on"] = []
                new_steps.append(step_dict)

            _save_todo(workdir, new_steps)
            logger.info(f"manage_todo: created TODO with {len(new_steps)} steps")
            return {
                "status": "ok",
                "action": "created",
                "step_count": len(new_steps),
                "todo_path": str(_todo_path(workdir)),
                "steps": new_steps,
            }

        elif action == "check":
            if step_id is None:
                return {
                    "status": "error",
                    "error": "step_id is required for check action",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            if step_id < 0 or step_id >= len(current):
                return {
                    "status": "error",
                    "error": f"step_id {step_id} out of range (0-{len(current) - 1})",
                }
            current[step_id]["done"] = True
            current[step_id]["status"] = "done"
            _save_todo(workdir, current)
            done_count = sum(1 for s in current if s.get("done"))
            logger.info(
                f"manage_todo: checked step {step_id} ({done_count}/{len(current)} done)"
            )
            return {
                "status": "ok",
                "action": "checked",
                "step_id": step_id,
                "done_count": done_count,
                "total": len(current),
                "steps": current,
            }

        elif action == "update":
            if step_id is None or description is None:
                return {
                    "status": "error",
                    "error": "step_id and description required for update",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            if step_id < 0 or step_id >= len(current):
                return {"status": "error", "error": f"step_id {step_id} out of range"}
            current[step_id]["description"] = description
            _save_todo(workdir, current)
            return {
                "status": "ok",
                "action": "updated",
                "step_id": step_id,
                "steps": current,
            }

        elif action == "read":
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "ok",
                    "steps": [],
                    "message": "No TODO list exists yet",
                }
            done_count = sum(1 for s in current if s.get("done"))
            return {
                "status": "ok",
                "steps": current,
                "done_count": done_count,
                "total": len(current),
                "todo_path": str(_todo_path(workdir)),
            }

        elif action == "clear":
            p = _todo_path(workdir)
            jp = _todo_json_path(workdir)
            if p.exists():
                p.unlink()
            if jp.exists():
                jp.unlink()
            logger.info("manage_todo: cleared TODO list")
            return {"status": "ok", "action": "cleared"}

        elif action == "start":
            if step_id is None:
                return {
                    "status": "error",
                    "error": "step_id is required for start action",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            if step_id < 0 or step_id >= len(current):
                return {
                    "status": "error",
                    "error": f"step_id {step_id} out of range (0-{len(current) - 1})",
                }
            # Only one step can be in_progress at a time
            active = [
                i for i, s in enumerate(current) if s.get("status") == "in_progress"
            ]
            if active and active[0] != step_id:
                return {
                    "status": "error",
                    "error": f"Step {active[0]} is already in_progress. Complete or block it first.",
                }
            current[step_id]["status"] = "in_progress"
            current[step_id]["started_at"] = (
                __import__("datetime").datetime.now().isoformat()
            )
            _save_todo(workdir, current)
            return {
                "status": "ok",
                "action": "started",
                "step_id": step_id,
                "steps": current,
            }

        elif action == "block":
            if step_id is None:
                return {
                    "status": "error",
                    "error": "step_id is required for block action",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            if step_id < 0 or step_id >= len(current):
                return {
                    "status": "error",
                    "error": f"step_id {step_id} out of range (0-{len(current) - 1})",
                }
            current[step_id]["status"] = "blocked"
            current[step_id]["blocked_reason"] = description or reason or ""
            _save_todo(workdir, current)
            return {
                "status": "ok",
                "action": "blocked",
                "step_id": step_id,
                "steps": current,
            }

        elif action == "verify":
            if step_id is None:
                return {
                    "status": "error",
                    "error": "step_id is required for verify action",
                }
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "error",
                    "error": "No TODO list found. Create one first.",
                }
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "error": f"step_id must be an integer, got {step_id!r}",
                }
            if step_id < 0 or step_id >= len(current):
                return {
                    "status": "error",
                    "error": f"step_id {step_id} out of range (0-{len(current) - 1})",
                }
            current[step_id]["status"] = "verified"
            current[step_id]["completed_at"] = (
                __import__("datetime").datetime.now().isoformat()
            )
            _save_todo(workdir, current)
            done_count = sum(
                1 for s in current if s.get("status") in ("done", "verified")
            )
            return {
                "status": "ok",
                "action": "verified",
                "step_id": step_id,
                "done_count": done_count,
                "total": len(current),
                "steps": current,
            }

        elif action == "next":
            current = _load_todo_json(workdir)
            if not current:
                return {
                    "status": "ok",
                    "next_step": None,
                    "message": "No TODO list found",
                }
            for i, step in enumerate(current):
                status = step.get("status", "pending")
                if status in ("done", "verified", "blocked"):
                    continue
                # Check if all dependencies are done
                deps = step.get("depends_on", [])
                all_deps_done = all(
                    current[d].get("status") in ("done", "verified")
                    for d in deps
                    if d < len(current)
                )
                if all_deps_done:
                    return {
                        "status": "ok",
                        "step_id": i,
                        "step": step,
                        "message": f"Next step: {step.get('description', '')}",
                    }
            return {
                "status": "ok",
                "next_step": None,
                "message": "All steps completed or blocked",
            }

        return {"status": "error", "error": f"Unhandled action '{action}'"}

    except Exception as e:
        logger.error(f"manage_todo: failed: {e}")
        return {"status": "error", "error": str(e)}
