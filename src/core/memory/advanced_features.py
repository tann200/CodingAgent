from __future__ import annotations

# ruff: noqa: E501

import json
import logging
import threading
import tempfile
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Lock protecting concurrent trajectory file writes (NEW-21)
_trajectory_lock = threading.Lock()

# Prefer canonical agent-context helpers when available; fall back to legacy
# ".agent-context" semantics for older workspaces. Keep this module resilient
# when src.tools.tools_config isn't importable (tests or lightweight runs).
try:
    from src.tools.tools_config import agent_context_path  # type: ignore
except Exception:
    agent_context_path = None  # type: ignore


class TrajectoryLogger:
    """Stores agent runs for training data generation and audit trails."""

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        # Resolve the canonical agent-context directory when possible; fall
        # back to the legacy ".agent-context" in the workspace root.
        ctx = (
            agent_context_path(self.workdir)
            if agent_context_path is not None
            else self.workdir / ".codingAgent"
        )
        self.context_dir = Path(ctx)
        self.trajectory_dir = self.context_dir / "trajectories"
        self.trajectory_dir.mkdir(parents=True, exist_ok=True)

    def log_run(
        self,
        task: str,
        plan: str,
        tool_sequence: List[Dict],
        patch: str,
        tests: str,
        success: bool,
        session_id: Optional[str] = None,
    ):
        """Log a complete agent run."""
        session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")

        trajectory = {
            "session_id": session_id,
            "timestamp": datetime.now().isoformat(),
            "task": task,
            "plan": plan,
            "tool_sequence": tool_sequence,
            "patch": patch,
            "tests": tests,
            "success": success,
        }

        filename = f"trajectory_{session_id}.json"
        filepath = self.trajectory_dir / filename

        with _trajectory_lock:
            # Ensure parent directory exists immediately before writing
            filepath.parent.mkdir(parents=True, exist_ok=True)

            # Prefer central atomic writer; fall back to simple write
            try:
                from src.core.io_utils import atomic_write_json

                logger.debug(
                    "TrajectoryLogger: attempting atomic_write_json for %s", filepath
                )
                ok = atomic_write_json(filepath, trajectory, logger=logger)
                if ok:
                    logger.info("Trajectory logged atomically: %s", filename)
                    return str(filepath)
                logger.warning(
                    "TrajectoryLogger: atomic_write_json returned False for %s; falling back",
                    filepath,
                )
            except Exception:
                import traceback

                logger.debug(
                    "TrajectoryLogger: atomic_write_json unavailable or failed for %s; falling back\n%s",
                    filepath,
                    traceback.format_exc(),
                )

            # mkstemp -> replace fallback to avoid leaving partial files
            fd, tmp_path = tempfile.mkstemp(dir=str(filepath.parent), suffix=".tmp")
            try:
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(trajectory, f, indent=2)
                except Exception:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    raise

                try:
                    os.replace(tmp_path, str(filepath))
                except Exception:
                    shutil.move(tmp_path, str(filepath))
            except Exception:
                # Last resort: write directly (best-effort)
                try:
                    filepath.write_text(
                        json.dumps(trajectory, indent=2), encoding="utf-8"
                    )
                except Exception:
                    logger.exception(
                        "TrajectoryLogger: failed to write trajectory to %s", filepath
                    )

        logger.info(f"Trajectory logged: {filename}")
        return str(filepath)

    def get_recent_trajectories(self, limit: int = 10) -> List[Dict]:
        """Get recent trajectories."""
        trajectories = []

        for f in sorted(self.trajectory_dir.glob("trajectory_*.json"), reverse=True)[
            :limit
        ]:
            try:
                with open(f) as fp:
                    trajectories.append(json.load(fp))
            except Exception:
                pass

        return trajectories

    def get_successful_trajectories(self, limit: int = 50) -> List[Dict]:
        """Get successful trajectories for training."""
        successful = []

        for f in self.trajectory_dir.glob("trajectory_*.json"):
            try:
                with open(f) as fp:
                    data = json.load(fp)
                    if data.get("success"):
                        successful.append(data)
            except Exception:
                pass

        return successful[:limit]

    def export_training_data(self, output_path: Optional[str] = None) -> str:
        """Export all trajectories as training data."""
        trajectories = self.get_recent_trajectories(limit=1000)

        output = (
            Path(output_path) if output_path else self.workdir / "training_data.json"
        )

        # Ensure parent dir exists immediately before writing
        output.parent.mkdir(parents=True, exist_ok=True)

        try:
            from src.core.io_utils import atomic_write_json

            logger.debug(
                "TrajectoryLogger: attempting atomic_write_json for %s", output
            )
            ok = atomic_write_json(output, trajectories, logger=logger)
            if ok:
                logger.info("Training data exported atomically: %s", output)
                return str(output)
            logger.warning(
                "TrajectoryLogger: atomic_write_json returned False for %s; falling back",
                output,
            )
        except Exception:
            import traceback

            logger.debug(
                "TrajectoryLogger: atomic_write_json unavailable or failed for %s; falling back\n%s",
                output,
                traceback.format_exc(),
            )

        # mkstemp -> replace fallback
        fd, tmp_path = tempfile.mkstemp(dir=str(output.parent), suffix=".tmp")
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(trajectories, f, indent=2)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise

            try:
                os.replace(tmp_path, str(output))
            except Exception:
                shutil.move(tmp_path, str(output))
        except Exception:
            try:
                output.write_text(json.dumps(trajectories, indent=2), encoding="utf-8")
            except Exception:
                logger.exception(
                    "TrajectoryLogger: failed to export training data to %s", output
                )

        return str(output)
