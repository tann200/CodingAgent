from __future__ import annotations
import json
import logging
import traceback
import tempfile
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.tools._path_utils import safe_resolve as _safe_resolve
from src.tools._tool import tool
from src.tools.tools_config import agent_context_path

logger = logging.getLogger(__name__)


@tool(side_effects=["write"], tags=["planning"])
def create_state_checkpoint(
    current_task: str,
    tool_call_history: List[Dict[str, Any]],
    modified_files: List[str],
    reasoning_summary: str,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a state checkpoint for long-running agent sessions.

    Args:
        current_task: Description of current task
        tool_call_history: List of tool calls made so far
        modified_files: List of files that were modified
        reasoning_summary: Summary of agent reasoning so far
        workdir: Working directory path

    Returns:
        status, checkpoint_id, checkpoint_path
    """
    wd = Path(workdir) if workdir else Path.cwd()
    checkpoint_dir = agent_context_path(wd) / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    checkpoint_id = f"checkpoint_{timestamp}"
    checkpoint_path = checkpoint_dir / f"{checkpoint_id}.json"

    checkpoint_data = {
        "checkpoint_id": checkpoint_id,
        "created_at": datetime.now().isoformat(),
        "current_task": current_task,
        "tool_call_history": tool_call_history,
        "modified_files": modified_files,
        "reasoning_summary": reasoning_summary,
        "task_history": [],
    }
    try:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

        # Prefer central atomic writer when available
        try:
            from src.core.io_utils import atomic_write_json

            logger.debug(
                "state_tools: attempting atomic_write_json for %s", checkpoint_path
            )
            ok = atomic_write_json(checkpoint_path, checkpoint_data, logger=logger)
            if ok:
                return {
                    "status": "ok",
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_path": str(checkpoint_path),
                }
            logger.warning(
                "state_tools: atomic_write_json returned False for %s; falling back",
                checkpoint_path,
            )
        except Exception:
            logger.debug(
                "state_tools: atomic_write_json unavailable or failed for %s; falling back\n%s",
                checkpoint_path,
                traceback.format_exc(),
            )

        # Fallback: mkstemp + atomic replace
        fd, tmp_path = tempfile.mkstemp(dir=str(checkpoint_path.parent), suffix=".tmp")
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(checkpoint_data, f, indent=2)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise

            try:
                os.replace(tmp_path, str(checkpoint_path))
            except Exception:
                try:
                    shutil.move(tmp_path, str(checkpoint_path))
                except Exception as e:
                    import traceback as _tb

                    logger.error(
                        "state_tools: fallback write failed for %s: %s\n%s",
                        checkpoint_path,
                        e,
                        _tb.format_exc(),
                    )
                    # Final fallback to Path.write_text
                    try:
                        checkpoint_path.write_text(
                            json.dumps(checkpoint_data, indent=2), encoding="utf-8"
                        )
                    except Exception:
                        logger.error(
                            "state_tools: final fallback write_text failed for %s\n%s",
                            checkpoint_path,
                            traceback.format_exc(),
                        )
        except Exception as e:
            logger.error(
                "state_tools: failed to save checkpoint %s: %s", checkpoint_path, e
            )
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass

        return {
            "status": "ok",
            "checkpoint_id": checkpoint_id,
            "checkpoint_path": str(checkpoint_path),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["planning"])
def list_checkpoints(workdir: Optional[str] = None) -> Dict[str, Any]:
    """List all available state checkpoints."""
    wd = Path(workdir) if workdir else Path.cwd()
    checkpoint_dir = agent_context_path(wd) / "checkpoints"

    if not checkpoint_dir.exists():
        return {"status": "ok", "checkpoints": []}

    checkpoints = []
    for f in sorted(checkpoint_dir.glob("checkpoint_*.json")):
        try:
            data = json.loads(f.read_text())
            checkpoints.append(
                {
                    "checkpoint_id": data.get("checkpoint_id"),
                    "created_at": data.get("created_at"),
                    "current_task": data.get("current_task", "")[:50],
                }
            )
        except Exception:
            pass

    return {"status": "ok", "checkpoints": checkpoints}


@tool(tags=["planning"])
def restore_state_checkpoint(
    checkpoint_id: str,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Restore a previous state checkpoint.

    Args:
        checkpoint_id: ID of checkpoint to restore
        workdir: Working directory path

    Returns:
        status, checkpoint_data or error
    """
    import re as _re

    if not _re.match(r"^[a-zA-Z0-9_\-]+$", checkpoint_id):
        return {
            "status": "error",
            "error": "Invalid checkpoint_id: only alphanumeric, underscore, and dash are allowed",
        }
    wd = Path(workdir) if workdir else Path.cwd()
    checkpoint_dir = agent_context_path(wd) / "checkpoints"
    checkpoint_path = checkpoint_dir / f"{checkpoint_id}.json"

    if not checkpoint_path.exists():
        return {"status": "error", "error": f"Checkpoint {checkpoint_id} not found"}

    try:
        data = json.loads(checkpoint_path.read_text())
        return {
            "status": "ok",
            "checkpoint_id": data.get("checkpoint_id"),
            "current_task": data.get("current_task"),
            "tool_call_history": data.get("tool_call_history"),
            "modified_files": data.get("modified_files"),
            "reasoning_summary": data.get("reasoning_summary"),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["review"])
def diff_state(
    checkpoint_id1: str,
    checkpoint_id2: str,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare two state checkpoints."""
    import re as _re

    _id_pattern = r"^[a-zA-Z0-9_\-]+$"
    if not _re.match(_id_pattern, checkpoint_id1) or not _re.match(
        _id_pattern, checkpoint_id2
    ):
        return {
            "status": "error",
            "error": "Invalid checkpoint_id: only alphanumeric, underscore, and dash are allowed",
        }
    wd = Path(workdir) if workdir else Path.cwd()
    # LOW-2 fix: use the same agent_context_path() helper as every other call
    # site in this file instead of a bare hardcoded ".agent-context" string.
    checkpoint_dir = agent_context_path(wd) / "checkpoints"

    try:
        cp1_path = checkpoint_dir / f"{checkpoint_id1}.json"
        cp2_path = checkpoint_dir / f"{checkpoint_id2}.json"

        if not cp1_path.exists() or not cp2_path.exists():
            return {"status": "error", "error": "One or both checkpoints not found"}

        cp1 = json.loads(cp1_path.read_text())
        cp2 = json.loads(cp2_path.read_text())

        diff = {
            "checkpoint1": checkpoint_id1,
            "checkpoint2": checkpoint_id2,
            "tasks_different": cp1.get("current_task") != cp2.get("current_task"),
            "tool_calls_added": len(cp2.get("tool_call_history", []))
            - len(cp1.get("tool_call_history", [])),
            "files_modified_added": list(
                set(cp2.get("modified_files", [])) - set(cp1.get("modified_files", []))
            ),
        }

        return {"status": "ok", "diff": diff}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@tool(tags=["coding"])
def batched_file_read(
    paths: List[str],
    workdir: Optional[str] = None,
    max_file_size: int = 10000,
) -> Dict[str, Any]:
    """
    Read multiple files in a single call, with size limits.

    Args:
        paths: List of file paths to read
        workdir: Working directory
        max_file_size: Maximum file size to read in bytes

    Returns:
        status, files (dict of path -> content or error)
    """
    wd = Path(workdir) if workdir else Path.cwd()
    results = {}

    for path in paths:
        try:
            try:
                p = _safe_resolve(path, wd)
            except PermissionError as _pe:
                results[path] = {"error": str(_pe)}
                continue
            if not p.exists():
                results[path] = {"error": "File not found"}
                continue

            if p.stat().st_size > max_file_size:
                results[path] = {"error": f"File too large ({p.stat().st_size} bytes)"}
                continue

            content = p.read_text(encoding="utf-8")
            results[path] = {"status": "ok", "content": content}
        except Exception as e:
            results[path] = {"error": str(e)}

    return {"status": "ok", "files": results, "count": len(paths)}


@tool(tags=["coding"])
def multi_file_summary(
    paths: List[str],
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get a quick summary of multiple files without reading full content.

    Args:
        paths: List of file paths to summarize
        workdir: Working directory

    Returns:
        status, summaries (dict of path -> file info)
    """
    wd = Path(workdir) if workdir else Path.cwd()
    results = {}

    for path in paths:
        try:
            try:
                p = _safe_resolve(path, wd)
            except PermissionError as pe:
                results[path] = {"error": str(pe)}
                continue
            if not p.exists():
                results[path] = {"error": "File not found"}
                continue

            stat = p.stat()
            results[path] = {
                "status": "ok",
                "size_bytes": stat.st_size,
                "size_lines": len(p.read_text(encoding="utf-8").splitlines())
                if stat.st_size < 100000
                else "unknown",
                "is_binary": not p.is_file() or b"\0" in p.read_bytes()[:100],
            }
        except Exception as e:
            results[path] = {"error": str(e)}

    return {"status": "ok", "summaries": results, "count": len(paths)}
