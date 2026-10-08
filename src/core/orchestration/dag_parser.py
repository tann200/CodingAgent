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
        """Build adjacency list from depends_on.

        H-01: Previously, references to non-existent step IDs were silently
        added to self.edges without a corresponding self.steps entry.
        get_ready_steps() would then never satisfy those phantom deps, causing
        topological_sort_waves() to raise "Circular dependency detected" with
        a misleading error message when the actual problem was a bad reference.

        Fix: validate each dep against self.steps and warn + skip if missing.
        """
        for step in self.steps.values():
            for dep in step.depends_on:
                if dep not in self.steps:
                    logger.warning(
                        "PlanDAG: step %r depends_on unknown step %r — dependency ignored",
                        step.step_id,
                        dep,
                    )
                    continue
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
        """Group steps into waves for parallel execution.

        Raises:
            ValueError: If a circular dependency is detected in the DAG.
        """
        waves = []
        completed: set[str] = set()
        remaining = set(self.steps.keys())

        while remaining:
            ready = self.get_ready_steps(completed)
            if not ready:
                cycle_members = sorted(remaining)
                logger.error(
                    "PlanDAG: circular dependency detected among steps %s. "
                    "Completed so far: %s",
                    cycle_members,
                    sorted(completed),
                )
                raise ValueError(
                    f"Circular dependency detected in plan DAG. "
                    f"Stuck steps: {cycle_members}"
                )

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
