from __future__ import annotations
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Lock protecting concurrent trajectory file writes (NEW-21)
_trajectory_lock = threading.Lock()


class TrajectoryLogger:
    """Stores agent runs for training data generation."""

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.trajectory_dir = self.workdir / ".agent-context" / "trajectories"
        self.trajectory_dir.mkdir(parents=True, exist_ok=True)

    def log_run(
        self,
        task: str,
        plan: str,
        tool_sequence: List[Dict],
        patch: str,
        tests: str,
        success: bool,
        session_id: str = None,
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

    def export_training_data(self, output_path: str = None) -> str:
        """Export all trajectories as training data."""
        trajectories = self.get_recent_trajectories(limit=1000)

        output = (
            Path(output_path) if output_path else self.workdir / "training_data.json"
        )

        with open(output, "w", encoding="utf-8") as f:
            json.dump(trajectories, f, indent=2)

        return str(output)


class DreamConsolidator:
    """Background memory consolidation to prevent vector store growth."""

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.memory_dir = self.workdir / ".agent-context" / "consolidated"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def consolidate_memories(self, vector_store_path: str = None) -> Dict[str, Any]:
        """Consolidate memories into higher-level knowledge."""
        consolidated = {
            "timestamp": datetime.now().isoformat(),
            "insights": [],
            "patterns": [],
        }

        task_state = self.workdir / ".agent-context" / "TASK_STATE.md"
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
        with open(summary_file, "w", encoding="utf-8") as f:
            json.dump(consolidated, f, indent=2)

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

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()

    def detect_code_smells(self, file_path: str) -> List[Dict]:
        """Detect common code smells in a file."""
        import ast

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

        smells_path = self.workdir / ".agent-context" / "code_smells.json"
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

    def __init__(self, workdir: str = None):
        self.workdir = Path(workdir) if workdir else Path.cwd()

    def review_patch(self, patch: str, context: str = None) -> Dict[str, Any]:
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
        review_path = self.workdir / ".agent-context" / "last_review.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(json.dumps(review, indent=2))
        return review_path


class SkillLearner:
    """Learns and creates new skills from successful task completion."""

    SKILL_DIR = Path("agent-brain/skills")

    def __init__(self, workdir: str = None):
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
