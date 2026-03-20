"""
TODO tracking tool for the coding agent.

Manages a human-readable TODO.md file at .agent-context/TODO.md so the user
can see task progress in real time and the agent can track which steps are done.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TODO_FILENAME = "TODO.md"
_TODO_JSON_FILENAME = "todo.json"


def _todo_path(workdir: str) -> Path:
    return Path(workdir) / ".agent-context" / _TODO_FILENAME


def _todo_json_path(workdir: str) -> Path:
    return Path(workdir) / ".agent-context" / _TODO_JSON_FILENAME


def _load_todo_json(workdir: str) -> List[Dict[str, Any]]:
    p = _todo_json_path(workdir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return []


def _save_todo(workdir: str, steps: List[Dict[str, Any]]) -> None:
    base = Path(workdir) / ".agent-context"
    base.mkdir(parents=True, exist_ok=True)

    # Save machine-readable JSON
    _todo_json_path(workdir).write_text(json.dumps(steps, indent=2))

    # Write human-readable Markdown
    lines = ["# Agent TODO\n"]
    for i, step in enumerate(steps):
        done = step.get("done", False)
        checkbox = "[x]" if done else "[ ]"
        desc = step.get("description", f"Step {i + 1}")
        lines.append(f"- {checkbox} **Step {i + 1}:** {desc}")

    _todo_path(workdir).write_text("\n".join(lines) + "\n")


def manage_todo(
    action: str,
    workdir: str,
    description: Optional[str] = None,
    step_id: Optional[int] = None,
    steps: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Manage the agent TODO list.

    Actions:
      - "create": Create a new TODO list from a list of step descriptions.
                  Pass: steps=["Step 1 description", "Step 2 description", ...]
      - "check":  Mark a step as completed.
                  Pass: step_id=<0-based index>
      - "update": Update a step's description.
                  Pass: step_id=<0-based index>, description="new description"
      - "read":   Return the current TODO list.
      - "clear":  Remove the TODO list (task complete).

    Args:
        action: One of "create", "check", "update", "read", "clear"
        workdir: Working directory (used to locate .agent-context/)
        description: New description for update action
        step_id: 0-based step index for check/update actions
        steps: List of step description strings for create action

    Returns:
        Dict with status and the current TODO state
    """
    try:
        if action == "create":
            if not steps:
                return {"status": "error", "error": "steps list is required for create action"}
            new_steps = [{"description": s, "done": False} for s in steps]
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
                return {"status": "error", "error": "step_id is required for check action"}
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {"status": "error", "error": f"step_id must be an integer, got {step_id!r}"}
            current = _load_todo_json(workdir)
            if not current:
                return {"status": "error", "error": "No TODO list found. Create one first."}
            if step_id < 0 or step_id >= len(current):
                return {"status": "error", "error": f"step_id {step_id} out of range (0-{len(current)-1})"}
            current[step_id]["done"] = True
            _save_todo(workdir, current)
            done_count = sum(1 for s in current if s.get("done"))
            logger.info(f"manage_todo: checked step {step_id} ({done_count}/{len(current)} done)")
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
                return {"status": "error", "error": "step_id and description required for update"}
            try:
                step_id = int(step_id)
            except (TypeError, ValueError):
                return {"status": "error", "error": f"step_id must be an integer, got {step_id!r}"}
            current = _load_todo_json(workdir)
            if not current:
                return {"status": "error", "error": "No TODO list found. Create one first."}
            if step_id < 0 or step_id >= len(current):
                return {"status": "error", "error": f"step_id {step_id} out of range"}
            current[step_id]["description"] = description
            _save_todo(workdir, current)
            return {"status": "ok", "action": "updated", "step_id": step_id, "steps": current}

        elif action == "read":
            current = _load_todo_json(workdir)
            if not current:
                return {"status": "ok", "steps": [], "message": "No TODO list exists yet"}
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

        else:
            return {
                "status": "error",
                "error": f"Unknown action '{action}'. Use: create, check, update, read, clear",
            }

    except Exception as e:
        logger.error(f"manage_todo: failed: {e}")
        return {"status": "error", "error": str(e)}
