"""
Scenario Evaluation Framework for CodingAgent.

This module provides a standardized way to evaluate the agent on coding tasks,
similar to SWE-bench style evaluations.
"""

import json
import logging
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class Scenario:
    """A test scenario for evaluation."""

    name: str
    description: str
    task: str
    setup_files: Dict[str, str] = field(default_factory=dict)
    expected_files: Dict[str, str] = field(default_factory=dict)
    test_command: Optional[str] = None
    cleanup_command: Optional[str] = None
    # M5: Metadata for filtering and reporting
    difficulty: str = "medium"
    category: str = "general"
    tags: List[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    """Result of a scenario evaluation."""

    scenario_name: str
    status: str  # "pass", "fail", "error"
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    tool_calls: List[Dict] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    error: Optional[str] = None
    verification_output: Optional[str] = None


class ScenarioEvaluator:
    """
    Evaluates CodingAgent on standardized scenarios.

    Usage:
        evaluator = ScenarioEvaluator()

        # Add scenarios
        evaluator.add_scenario(Scenario(
            name="simple_function",
            description="Create a simple function",
            task="Create a function hello() that returns 'Hello World'",
            expected_files={"hello.py": "def hello():\n    return 'Hello World'"},
            test_command="python -c 'from hello import hello; assert hello() == \"Hello World\"'",
        ))

        # Run evaluation
        results = evaluator.run_evaluation(agent_factory)

        # Get summary
        summary = evaluator.get_summary(results)
    """

    def __init__(self, workdir: Optional[str] = None):
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp())
        self.scenarios: List[Scenario] = []
        self.results: List[ScenarioResult] = []

    def add_scenario(self, scenario: Scenario):
        """Add a scenario to the evaluation suite."""
        self.scenarios.append(scenario)
        logger.info(f"Added scenario: {scenario.name}")

    def add_scenarios_from_file(self, filepath: str):
        """Load scenarios from a JSON file."""
        with open(filepath, "r") as f:
            data = json.load(f)
            for item in data.get("scenarios", []):
                self.add_scenario(
                    Scenario(
                        name=item["name"],
                        description=item.get("description", ""),
                        task=item["task"],
                        setup_files=item.get("setup_files", {}),
                        expected_files=item.get("expected_files", {}),
                        test_command=item.get("test_command"),
                        cleanup_command=item.get("cleanup_command"),
                        difficulty=item.get("difficulty", "medium"),
                        category=item.get("category", "general"),
                        tags=item.get("tags", []),
                    )
                )

    def _setup_scenario(self, scenario: Scenario) -> Path:
        """Setup scenario files in a temporary directory."""
        scenario_dir = self.workdir / scenario.name
        scenario_dir.mkdir(parents=True, exist_ok=True)

        for filename, content in scenario.setup_files.items():
            filepath = scenario_dir / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)

        return scenario_dir

    def _verify_scenario(
        self, scenario: Scenario, scenario_dir: Path
    ) -> tuple[bool, str]:
        """Verify scenario results."""
        output = ""

        # Check expected files exist with correct content
        for filename, expected_content in scenario.expected_files.items():
            filepath = scenario_dir / filename
            if not filepath.exists():
                return False, f"Expected file not found: {filename}"

            actual_content = filepath.read_text()
            if expected_content.strip() not in actual_content.strip():
                return False, f"File content mismatch: {filename}"

        # Run test command if provided (shell=False for security — C11 fix)
        if scenario.test_command:
            try:
                cmd_parts = shlex.split(scenario.test_command)
                result = subprocess.run(
                    cmd_parts,
                    shell=False,
                    cwd=scenario_dir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                output = result.stdout + result.stderr
                if result.returncode != 0:
                    return False, f"Test command failed: {output}"
            except subprocess.TimeoutExpired:
                return False, "Test command timed out"
            except Exception as e:
                return False, f"Test command error: {str(e)}"

        return True, output

    def run_scenario(
        self,
        scenario: Scenario,
        agent_factory: Callable[[], Any],
    ) -> ScenarioResult:
        """Run a single scenario."""
        start_time = datetime.now()
        scenario_dir = None

        try:
            # Setup
            scenario_dir = self._setup_scenario(scenario)

            # Run agent on task (C7 fix — agent_factory result is now actually invoked)
            logger.info(f"Running scenario: {scenario.name}")
            agent = agent_factory()
            try:
                if hasattr(agent, "run"):
                    agent.run(scenario.task, working_dir=str(scenario_dir))
                elif callable(agent):
                    agent(scenario.task)
                else:
                    logger.warning(
                        f"ScenarioEvaluator: agent has no 'run' method and is not callable — skipping agent execution for '{scenario.name}'"
                    )
            except Exception as agent_err:
                logger.warning(
                    f"ScenarioEvaluator: agent raised during scenario '{scenario.name}': {agent_err}"
                )

            # Verify
            passed, verification_output = self._verify_scenario(scenario, scenario_dir)

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            return ScenarioResult(
                scenario_name=scenario.name,
                status="pass" if passed else "fail",
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                verification_output=verification_output,
            )

        except Exception as e:
            end_time = datetime.now()
            return ScenarioResult(
                scenario_name=scenario.name,
                status="error",
                start_time=start_time,
                end_time=end_time,
                duration_seconds=(end_time - start_time).total_seconds(),
                error=str(e),
            )
        finally:
            # Cleanup
            if scenario_dir and scenario.cleanup_command:
                try:
                    subprocess.run(
                        shlex.split(scenario.cleanup_command),
                        shell=False,
                        cwd=scenario_dir,
                        capture_output=True,
                        timeout=30,
                    )
                except Exception as e:
                    logger.warning(f"Cleanup failed for {scenario.name}: {e}")

    def run_evaluation(
        self,
        agent_factory: Callable[[], Any],
    ) -> List[ScenarioResult]:
        """Run all scenarios."""
        self.results = []

        logger.info(f"Starting evaluation with {len(self.scenarios)} scenarios")

        for scenario in self.scenarios:
            result = self.run_scenario(scenario, agent_factory)
            self.results.append(result)
            logger.info(f"Scenario {scenario.name}: {result.status}")

        return self.results

    def get_summary(
        self, results: Optional[List[ScenarioResult]] = None
    ) -> Dict[str, Any]:
        """Get evaluation summary."""
        results = results or self.results

        passed = sum(1 for r in results if r.status == "pass")
        failed = sum(1 for r in results if r.status == "fail")
        errors = sum(1 for r in results if r.status == "error")

        total_duration = sum(r.duration_seconds for r in results)

        return {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "pass_rate": passed / len(results) if results else 0,
            "total_duration_seconds": total_duration,
            "average_duration_seconds": total_duration / len(results) if results else 0,
        }

    def get_summary_by_category(
        self, results: Optional[List[ScenarioResult]] = None
    ) -> Dict[str, Any]:
        """M5: Break summary down by scenario category and difficulty."""
        results = results or self.results
        # Build name→scenario map for metadata lookup
        sc_map = {s.name: s for s in self.scenarios}

        by_category: Dict[str, Dict[str, int]] = {}
        by_difficulty: Dict[str, Dict[str, int]] = {}
        for r in results:
            sc = sc_map.get(r.scenario_name)
            cat = sc.category if sc else "unknown"
            diff = sc.difficulty if sc else "unknown"

            by_category.setdefault(cat, {"pass": 0, "fail": 0, "error": 0})
            by_difficulty.setdefault(diff, {"pass": 0, "fail": 0, "error": 0})
            by_category[cat][r.status] = by_category[cat].get(r.status, 0) + 1
            by_difficulty[diff][r.status] = by_difficulty[diff].get(r.status, 0) + 1

        return {
            "by_category": by_category,
            "by_difficulty": by_difficulty,
        }

    def export_results(self, filepath: str):
        """Export results to JSON file."""
        data = {
            "summary": self.get_summary(),
            "results": [
                {
                    "scenario_name": r.scenario_name,
                    "status": r.status,
                    "start_time": r.start_time.isoformat(),
                    "end_time": r.end_time.isoformat(),
                    "duration_seconds": r.duration_seconds,
                    "error": r.error,
                    "verification_output": r.verification_output,
                }
                for r in self.results
            ],
        }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Results exported to {filepath}")


# Built-in scenarios
def get_default_scenarios() -> List[Scenario]:
    """Get a set of default evaluation scenarios."""
    return [
        Scenario(
            name="simple_function",
            description="Create a simple function that returns a string",
            task="Create a file called hello.py with a function hello() that returns 'Hello World'",
            expected_files={"hello.py": "def hello():\n    return 'Hello World'"},
            test_command="python -c \"from hello import hello; assert hello() == 'Hello World'\"",
        ),
        Scenario(
            name="class_definition",
            description="Create a class with methods",
            task="Create a file calculator.py with a Calculator class that has add and subtract methods",
            expected_files={"calculator.py": "class Calculator:"},
            test_command='python -c "from calculator import Calculator; c = Calculator(); assert c.add(1, 2) == 3"',
        ),
        Scenario(
            name="test_creation",
            description="Create a simple unit test",
            task="Create a test file test_math.py with a test for a math function",
            expected_files={"test_math.py": "import pytest"},
        ),
    ]


def run_benchmark(
    agent_factory: Callable[[], Any],
    scenarios: Optional[List[Scenario]] = None,
) -> Dict[str, Any]:
    """Run a quick benchmark with default scenarios."""
    evaluator = ScenarioEvaluator()

    if scenarios is None:
        scenarios = get_default_scenarios()

    for scenario in scenarios:
        evaluator.add_scenario(scenario)

    results = evaluator.run_evaluation(agent_factory)
    summary = evaluator.get_summary(results)

    return summary
