from __future__ import annotations

# ruff: noqa: E501

import ast
import json
import logging
import threading
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
    """Stores agent runs for training data generation."""

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        # Resolve the canonical agent-context directory when possible; fall
        # back to the legacy ".agent-context" in the workspace root.
        ctx = (
            agent_context_path(self.workdir)
            if agent_context_path is not None
            else self.workdir / ".agent-context"
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

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(trajectory, f, indent=2)

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

        with open(output, "w", encoding="utf-8") as f:
            json.dump(trajectories, f, indent=2)

        return str(output)


class DreamConsolidator:
    """Background memory consolidation to prevent vector store growth."""

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        ctx = (
            agent_context_path(self.workdir)
            if agent_context_path is not None
            else self.workdir / ".agent-context"
        )
        self.context_dir = Path(ctx)
        self.memory_dir = self.context_dir / "consolidated"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def consolidate_memories(
        self, vector_store_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Consolidate memories into higher-level knowledge."""
        consolidated = {
            "timestamp": datetime.now().isoformat(),
            "insights": [],
            "patterns": [],
        }

        task_state = self.context_dir / "TASK_STATE.md"
        if task_state.exists():
            content = task_state.read_text(encoding="utf-8")

            if "def " in content or "class " in content:
                consolidated["patterns"].append("code_generation")

            # Use more specific patterns to avoid false positives on section headings
            if any(
                p in content.lower()
                for p in [
                    "traceback",
                    "exception:",
                    "fixerror",
                    "fix error",
                    "debug attempt",
                ]
            ):
                consolidated["patterns"].append("error_recovery")

            if "test" in content.lower():
                consolidated["patterns"].append("test_driven")

        summary_file = (
            self.memory_dir / f"consolidated_{datetime.now().strftime('%Y%m%d')}.json"
        )
        # Ensure parent exists and prefer atomic write
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            from src.core.io_utils import atomic_write_json

            logger.debug(
                "DreamConsolidator: attempting atomic_write_json for %s", summary_file
            )
            ok = atomic_write_json(summary_file, consolidated, logger=logger)
            if not ok:
                # Fallback
                with open(summary_file, "w", encoding="utf-8") as f:
                    json.dump(consolidated, f, indent=2)
        except Exception:
            try:
                with open(summary_file, "w", encoding="utf-8") as f:
                    json.dump(consolidated, f, indent=2)
            except Exception:
                logger.exception(
                    "DreamConsolidator: failed to write consolidated memory to %s",
                    summary_file,
                )

        return consolidated

    def get_consolidated_knowledge(self) -> List[Dict]:
        """Retrieve consolidated knowledge."""
        knowledge = []

        for f in sorted(self.memory_dir.glob("consolidated_*.json"), reverse=True)[:10]:
            try:
                with open(f) as fp:
                    knowledge.append(json.load(fp))
            except Exception:
                pass

        return knowledge


class RefactoringAgent:
    """Autonomous refactoring for code quality improvement."""

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()

    def detect_code_smells(self, file_path: str) -> List[Dict]:
        """Detect common code smells in a file."""
        p = self.workdir / file_path
        if not p.exists():
            return []

        smells = []

        try:
            source = p.read_text(encoding="utf-8")
            tree = ast.parse(source)

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Use actual line span (end_lineno - lineno) rather than body node count
                    line_span = (
                        getattr(node, "end_lineno", node.lineno) - node.lineno + 1
                    )
                    if line_span > 50:
                        smells.append(
                            {
                                "type": "long_function",
                                "file_path": str(p.relative_to(self.workdir)),
                                "name": node.name,
                                "line": node.lineno,
                                "severity": "medium",
                                "suggestion": f"Function {node.name} is {line_span} lines long. Consider splitting.",
                            }
                        )

                    if len(node.args.args) > 6:
                        smells.append(
                            {
                                "type": "too_many_parameters",
                                "file_path": str(p.relative_to(self.workdir)),
                                "name": node.name,
                                "line": node.lineno,
                                "severity": "low",
                                "suggestion": f"Function {node.name} has {len(node.args.args)} parameters.",
                            }
                        )

                if isinstance(node, ast.ClassDef):
                    method_count = sum(
                        1
                        for n in node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    )
                    if method_count > 20:
                        smells.append(
                            {
                                "type": "large_class",
                                "file_path": str(p.relative_to(self.workdir)),
                                "name": node.name,
                                "line": node.lineno,
                                "severity": "medium",
                                "suggestion": f"Class {node.name} has {method_count} methods.",
                            }
                        )

        except Exception as e:
            logger.warning(f"Failed to analyze {file_path}: {e}")

        return smells

    def save_smells(self, smells: List[Dict], append: bool = True) -> Optional[Path]:
        """Save detected smells to .agent-context/code_smells.json."""
        if not smells:
            return None

        # Resolve location in the canonical agent-context directory when
        # available; otherwise fall back to legacy path.
        ctx = (
            agent_context_path(self.workdir)
            if agent_context_path is not None
            else self.workdir / ".agent-context"
        )
        smells_path = Path(ctx) / "code_smells.json"
        smells_path.parent.mkdir(parents=True, exist_ok=True)

        existing_smells = {}
        if append and smells_path.exists():
            try:
                existing_smells = json.loads(smells_path.read_text())
            except Exception:
                pass

        for smell in smells:
            file_path = smell.get("file_path")
            if file_path:
                if file_path not in existing_smells:
                    existing_smells[file_path] = []
                existing_smells[file_path].append(smell)

        # Prefer atomic write
        try:
            from src.core.io_utils import atomic_write_json

            logger.debug(
                "RefactoringAgent: attempting atomic_write_json for %s", smells_path
            )
            ok = atomic_write_json(smells_path, existing_smells, logger=logger)
            if ok:
                return smells_path
            logger.warning(
                "RefactoringAgent: atomic_write_json returned False for %s; falling back",
                smells_path,
            )
        except Exception:
            import traceback

            logger.debug(
                "RefactoringAgent: atomic_write_json unavailable for %s; falling back\n%s",
                smells_path,
                traceback.format_exc(),
            )

        smells_path.write_text(json.dumps(existing_smells, indent=2))
        return smells_path

    def suggest_refactoring(self, file_path: str) -> Dict[str, Any]:
        """Generate refactoring suggestions."""
        smells = self.detect_code_smells(file_path)

        return {
            "file": file_path,
            "smell_count": len(smells),
            "smells": smells,
            "can_auto_fix": any(
                s.get("type") in ["long_function", "too_many_parameters"]
                for s in smells
            ),
        }


class ReviewAgent:
    """Multi-agent code review capability."""

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()

    def review_patch(self, patch: str, context: Optional[str] = None) -> Dict[str, Any]:
        """Review a patch and provide feedback."""
        review = {
            "timestamp": datetime.now().isoformat(),
            "patch_length": len(patch.splitlines()),
            "issues": [],
            "recommendations": [],
        }

        if "TODO" in patch or "FIXME" in patch:
            review["issues"].append(
                {
                    "type": "unresolved_task",
                    "severity": "low",
                    "message": "Patch contains unresolved TODO/FIXME comments",
                }
            )

        if len(patch.splitlines()) > 100:
            review["recommendations"].append(
                {
                    "type": "large_patch",
                    "message": "Consider breaking into smaller, reviewable chunks",
                }
            )

        if (
            "password" in patch.lower()
            or "secret" in patch.lower()
            or "api_key" in patch.lower()
        ):
            review["issues"].append(
                {
                    "type": "security",
                    "severity": "high",
                    "message": "Potential hardcoded secrets detected",
                }
            )

        review["overall"] = (
            "approved"
            if not any(i.get("severity") == "high" for i in review["issues"])
            else "needs_changes"
        )

        return review

    def save_review(self, review: Dict[str, Any], append: bool = False) -> Path:
        """Save review to .agent-context/last_review.json."""
        ctx = (
            agent_context_path(self.workdir)
            if agent_context_path is not None
            else self.workdir / ".agent-context"
        )
        review_path = Path(ctx) / "last_review.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from src.core.io_utils import atomic_write_json

            logger.debug(
                "ReviewAgent: attempting atomic_write_json for %s", review_path
            )
            ok = atomic_write_json(review_path, review, logger=logger)
            if ok:
                return review_path
            logger.warning(
                "ReviewAgent: atomic_write_json returned False for %s; falling back",
                review_path,
            )
        except Exception:
            import traceback

            logger.debug(
                "ReviewAgent: atomic_write_json unavailable for %s; falling back\n%s",
                review_path,
                traceback.format_exc(),
            )

        review_path.write_text(json.dumps(review, indent=2))
        return review_path


class SkillLearner:
    """Learns and creates new skills from successful task completion."""

    SKILL_DIR = Path("agent-brain/skills")

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.skill_dir = self.workdir / self.SKILL_DIR
        self.skill_dir.mkdir(parents=True, exist_ok=True)

    def create_skill(
        self, name: str, description: str, patterns: List[str], examples: List[Dict]
    ) -> str:
        """Create a new skill file."""
        content = f"""# {name}

{description}

## Patterns

"""
        for pattern in patterns:
            content += f"- {pattern}\n"

        content += "\n## Examples\n\n"
        for i, example in enumerate(examples, 1):
            content += f"### Example {i}\n\n"
            content += f"Task: {example.get('task', '')}\n\n"
            content += f"Solution: {example.get('solution', '')}\n\n"

        content += f"---\n*Created: {datetime.now().isoformat()}*\n"

        safe_name = name.lower().replace(" ", "_")
        filepath = self.skill_dir / f"{safe_name}.md"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return str(filepath)

    def list_skills(self) -> List[str]:
        """List all available skills."""
        return [f.stem for f in self.skill_dir.glob("*.md")]

    def get_skill(self, name: str) -> Optional[str]:
        """Get skill content by name."""
        safe_name = name.lower().replace(" ", "_")
        filepath = self.skill_dir / f"{safe_name}.md"

        if filepath.exists():
            return filepath.read_text()
        return None
