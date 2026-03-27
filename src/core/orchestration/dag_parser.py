import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class StepNode:
    step_id: str
    description: str
    files: List[str]
    depends_on: List[str]
    status: str = "pending"


class PlanDAG:
    """Directed Acyclic Graph for task execution."""

    def __init__(self, steps: List[StepNode]):
        self.steps = {s.step_id: s for s in steps}
        self.edges: Dict[str, Set[str]] = {}
        self._build_edges()

    def _build_edges(self):
        """Build adjacency list from depends_on."""
        for step in self.steps.values():
            for dep in step.depends_on:
                if dep not in self.edges:
                    self.edges[dep] = set()
                self.edges[dep].add(step.step_id)

    def get_ready_steps(self, completed: Set[str]) -> List[str]:
        """Get steps where all dependencies are satisfied."""
        ready = []
        for step_id, step in self.steps.items():
            if step_id in completed:
                continue
            if all(dep in completed for dep in step.depends_on):
                ready.append(step_id)
        return ready

    def topological_sort_waves(self) -> List[List[str]]:
        """Group steps into waves for parallel execution."""
        waves = []
        completed = set()
        remaining = set(self.steps.keys())

        while remaining:
            ready = self.get_ready_steps(completed)
            if not ready:
                raise ValueError("Circular dependency detected")

            waves.append(ready)
            completed.update(ready)
            remaining -= set(ready)

        return waves

    def validate(self) -> bool:
        """Check for cycles and missing dependencies."""
        try:
            self.topological_sort_waves()
            return True
        except ValueError:
            return False

    @classmethod
    def from_todo_json(cls, todo_json_path: str) -> Optional["PlanDAG"]:
        """Load DAG from existing todo.json file."""
        from pathlib import Path

        path = Path(todo_json_path)
        if not path.exists():
            return None

        try:
            todo_data = json.loads(path.read_text())

            steps = [
                StepNode(
                    step_id=f"step_{item['id']}",
                    description=item.get("description", ""),
                    files=item.get("files", []),
                    depends_on=[f"step_{d}" for d in item.get("depends_on", [])],
                    status="complete" if item.get("done") else "pending",
                )
                for item in todo_data
            ]

            dag = cls(steps)
            if dag.validate():
                return dag
        except Exception:
            pass
        return None

    @classmethod
    def from_todo_markdown(
        cls, todo_path: str, todo_json_path: str
    ) -> Optional["PlanDAG"]:
        """Load DAG from TODO.md, with fallback to todo.json."""
        from pathlib import Path

        todo_md = Path(todo_path)
        todo_json = Path(todo_json_path)

        if not todo_md.exists():
            return cls.from_todo_json(str(todo_json))

        try:
            content = todo_md.read_text()
            steps = cls._parse_todo_markdown(content)

            if not steps:
                return cls.from_todo_json(str(todo_json))

            dag = cls(steps)
            if not dag.validate():
                logger.warning("PlanDAG: cycle detected, falling back to JSON")
                return cls.from_todo_json(str(todo_json))

            return dag

        except Exception as e:
            logger.error(f"PlanDAG.from_todo_markdown failed: {e}")
            return cls.from_todo_json(str(todo_json))

    @classmethod
    def _parse_todo_markdown(cls, content: str) -> List[StepNode]:
        """Parse TODO.md with depends_on syntax."""
        steps = []
        step_pattern = re.compile(
            r"^\s*[-*]\s*\[([ xX])\]\s*"
            r"\*?\s*\*?Step\s*(\d+):?\s*"
            r"(.+?)(?:\s*\((?:depends on|depends_on):\s*"
            r"((?:Steps?\s*\d+(?:\s*,\s*)?)+)\s*\))?"
            r"\s*$",
            re.IGNORECASE,
        )

        step_ref_pattern = re.compile(r"Steps?\s*(\d+)", re.IGNORECASE)

        for line in content.splitlines():
            match = step_pattern.match(line)
            if not match:
                continue

            checkbox, step_num, desc = match.groups()
            deps_match = match.group(4)

            step_id = f"step_{int(step_num) - 1}"
            depends_on = []

            if deps_match:
                dep_matches = step_ref_pattern.findall(deps_match)
                depends_on = [f"step_{int(d) - 1}" for d in dep_matches]

            steps.append(
                StepNode(
                    step_id=step_id,
                    description=desc.strip(),
                    files=[],
                    depends_on=depends_on,
                    status="complete" if checkbox.lower() == "x" else "pending",
                )
            )

        return steps

    def to_todo_format(self) -> List[Dict[str, Any]]:
        """Convert DAG back to todo format for manage_todo."""
        return [
            {
                "id": int(step.step_id.replace("step_", "")),
                "description": step.description,
                "done": step.status == "complete",
                "depends_on": [int(d.replace("step_", "")) for d in step.depends_on],
                "files": step.files,
            }
            for step in self.steps.values()
        ]

    def to_todo_markdown(self) -> str:
        """Convert DAG to Markdown format with depends_on."""
        lines = ["# Agent TODO\n"]

        for step in sorted(
            self.steps.values(), key=lambda s: int(s.step_id.replace("step_", ""))
        ):
            status = "x" if step.status == "complete" else " "
            step_num = int(step.step_id.replace("step_", "")) + 1

            if step.depends_on:
                dep_names = [
                    f"Step {int(d.replace('step_', '')) + 1}" for d in step.depends_on
                ]
                dep_str = ", ".join(dep_names)
                lines.append(
                    f"- [{status}] **Step {step_num}:** {step.description} (depends on: {dep_str})"
                )
            else:
                lines.append(f"- [{status}] **Step {step_num}:** {step.description}")

        return "\n".join(lines)

    def sync_to_files(self, todo_path: str, todo_json_path: str):
        """Bidirectional sync: writes both TODO.md and todo.json."""
        from pathlib import Path

        todo_md = Path(todo_path)
        todo_json = Path(todo_json_path)

        todo_md.parent.mkdir(parents=True, exist_ok=True)

        todo_md.write_text(self.to_todo_markdown())

        todo_data = self.to_todo_format()
        todo_json.write_text(json.dumps(todo_data, indent=2))

        logger.info(f"PlanDAG: synced to {todo_path} and {todo_json_path}")

    def apply_user_edit(self, markdown_content: str) -> bool:
        """Apply user edits from TODO.md and validate."""
        new_steps = self._parse_todo_markdown(markdown_content)

        new_dag = PlanDAG(new_steps)

        if not new_dag.validate():
            logger.error("PlanDAG: user edit created a cycle")
            return False

        self.steps = new_dag.steps
        self._build_edges()
        return True





def _parse_dag_content(content: str) -> Optional[PlanDAG]:
    """Parse LLM output into PlanDAG."""
    json_match = re.search(r"\{[\s\S]*\}", content)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            steps = [
                StepNode(
                    step_id=s["step_id"],
                    description=s["description"],
                    files=s.get("files", []),
                    depends_on=s.get("depends_on", []),
                )
                for s in data.get("steps", [])
            ]
            dag = PlanDAG(steps)
            if dag.validate():
                return dag
        except (json.JSONDecodeError, KeyError):
            pass

    return None


def _convert_flat_to_dag(flat_plan: List[Dict]) -> PlanDAG:
    """Convert flat plan (no dependencies) to DAG."""
    steps = [
        StepNode(
            step_id=f"step_{i}",
            description=s.get("description", f"Step {i}"),
            files=s.get("files", []),
            depends_on=[],
        )
        for i, s in enumerate(flat_plan)
    ]
    return PlanDAG(steps)
