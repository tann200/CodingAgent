"""
Scenario Evaluation Framework for CodingAgent.

This module provides a standardized way to evaluate the agent on coding tasks,
similar to SWE-bench style evaluations.
"""

import json
import logging
import math
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


def _align_agent_working_dir(agent: Any, scenario_dir: Path) -> None:
    """Point agent-backed runs at the scenario directory when supported."""
    if not hasattr(agent, "working_dir"):
        return
    try:
        current = getattr(agent, "working_dir")
        aligned = scenario_dir if isinstance(current, Path) or current is None else str(scenario_dir)
        setattr(agent, "working_dir", aligned)
    except Exception:
        return
    try:
        ensure_working_dir = getattr(agent, "_ensure_working_dir", None)
        if callable(ensure_working_dir):
            ensure_working_dir()
    except Exception as exc:
        logger.debug("scenario_evaluator: _ensure_working_dir failed: %s", exc)


def _run_agent_for_scenario(agent: Any, scenario: "Scenario", scenario_dir: Path) -> None:
    """Execute one scenario against an agent-like object.

    Supports the existing direct-agent ``run(...)`` contract and the real
    ``Orchestrator.run_agent_once(...)`` path used by benchmarks.
    """
    _align_agent_working_dir(agent, scenario_dir)

    if hasattr(agent, "run"):
        agent.run(scenario.task, working_dir=str(scenario_dir))
        return

    if hasattr(agent, "run_agent_once") and callable(getattr(agent, "run_agent_once")):
        tools = {}
        try:
            tools = agent.get_tools_for_role("default")
        except Exception:
            tools = getattr(agent, "tools", {}) or {}

        agent.run_agent_once(
            system_prompt_name="operational",
            messages=[{"role": "user", "content": scenario.task}],
            tools=tools,
        )
        return

    if callable(agent):
        agent(scenario.task)
        return

    logger.warning(
        "ScenarioEvaluator: agent has no supported execution surface for '%s'",
        scenario.name,
    )


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
                _run_agent_for_scenario(agent, scenario, scenario_dir)
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
    """Get a set of default evaluation scenarios.

    P4-T1: Extended from 3 trivial scenarios to 23 covering:
    single-file edits, multi-file refactoring, bug fix, task disambiguation,
    tool failure recovery, plan adherence, context boundary, and delegation.
    """
    return [
        # ── Original 3 ──────────────────────────────────────────────────────
        Scenario(
            name="simple_function",
            description="Create a simple function that returns a string",
            task="Create a file called hello.py with a function hello() that returns 'Hello World'",
            expected_files={"hello.py": "def hello():\n    return 'Hello World'"},
            test_command="python -c \"from hello import hello; assert hello() == 'Hello World'\"",
            difficulty="easy",
            category="single_file_edit",
        ),
        Scenario(
            name="class_definition",
            description="Create a class with methods",
            task="Create a file calculator.py with a Calculator class that has add and subtract methods",
            expected_files={"calculator.py": "class Calculator:"},
            test_command='python -c "from calculator import Calculator; c = Calculator(); assert c.add(1, 2) == 3"',
            difficulty="easy",
            category="single_file_edit",
        ),
        Scenario(
            name="test_creation",
            description="Create a simple unit test",
            task="Create a test file test_math.py with a test for a math function",
            expected_files={"test_math.py": "import pytest"},
            difficulty="easy",
            category="single_file_edit",
        ),

        # ── Single-file edits ────────────────────────────────────────────────
        Scenario(
            name="rename_function",
            description="Rename a function in an existing file",
            task="Rename the function `compute_total` to `calculate_total` in utils.py",
            setup_files={
                "utils.py": (
                    "def compute_total(items):\n"
                    "    return sum(items)\n"
                ),
            },
            expected_files={"utils.py": "def calculate_total"},
            difficulty="easy",
            category="single_file_edit",
            tags=["rename"],
        ),
        Scenario(
            name="add_docstring",
            description="Add a docstring to an existing function",
            task="Add a Google-style docstring to the function `parse_config` in config.py that describes what it does and its parameters.",
            setup_files={
                "config.py": (
                    "def parse_config(path):\n"
                    "    with open(path) as f:\n"
                    "        return f.read()\n"
                ),
            },
            expected_files={"config.py": '"""'},
            difficulty="easy",
            category="single_file_edit",
            tags=["docstring"],
        ),
        Scenario(
            name="fix_off_by_one",
            description="Fix an off-by-one error in a loop",
            task=(
                "Fix the off-by-one error in the `get_last_n` function in list_utils.py. "
                "The function should return the last n items of a list but currently misses the last element."
            ),
            setup_files={
                "list_utils.py": (
                    "def get_last_n(items, n):\n"
                    "    return items[len(items)-n:len(items)-1]\n"  # bug: -1 drops last
                ),
            },
            expected_files={"list_utils.py": "def get_last_n"},
            test_command=(
                "python -c \""
                "from list_utils import get_last_n; "
                "assert get_last_n([1,2,3,4,5], 3) == [3,4,5], 'off-by-one not fixed'\""
            ),
            difficulty="medium",
            category="single_file_edit",
            tags=["bug_fix", "off_by_one"],
        ),
        Scenario(
            name="add_type_hints",
            description="Add type hints to an untyped function",
            task="Add Python type hints to all parameters and the return type of the `merge_dicts` function in merge.py.",
            setup_files={
                "merge.py": (
                    "def merge_dicts(a, b):\n"
                    "    result = {}\n"
                    "    result.update(a)\n"
                    "    result.update(b)\n"
                    "    return result\n"
                ),
            },
            expected_files={"merge.py": "def merge_dicts"},
            difficulty="easy",
            category="single_file_edit",
            tags=["type_hints"],
        ),
        Scenario(
            name="add_error_handling",
            description="Add try/except error handling to a function",
            task=(
                "Add proper error handling to `read_json_file` in file_reader.py. "
                "It should raise a ValueError with a descriptive message if the file "
                "does not exist or contains invalid JSON."
            ),
            setup_files={
                "file_reader.py": (
                    "import json\n\n"
                    "def read_json_file(path):\n"
                    "    with open(path) as f:\n"
                    "        return json.load(f)\n"
                ),
            },
            expected_files={"file_reader.py": "ValueError"},
            difficulty="medium",
            category="single_file_edit",
            tags=["error_handling"],
        ),

        # ── Multi-file refactoring ───────────────────────────────────────────
        Scenario(
            name="move_class_update_imports",
            description="Move a class to a new module and update all import references",
            task=(
                "Move the `UserValidator` class from validators.py into a new file "
                "user_validator.py. Update the import in app.py so it imports "
                "`UserValidator` from `user_validator` instead of `validators`."
            ),
            setup_files={
                "validators.py": (
                    "class UserValidator:\n"
                    "    def validate(self, user):\n"
                    "        return bool(user.get('name'))\n"
                ),
                "app.py": (
                    "from validators import UserValidator\n\n"
                    "def process(user):\n"
                    "    v = UserValidator()\n"
                    "    return v.validate(user)\n"
                ),
            },
            expected_files={
                "user_validator.py": "class UserValidator",
                "app.py": "from user_validator import UserValidator",
            },
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "move_class"],
        ),
        Scenario(
            name="split_module",
            description="Split a monolithic module into two focused modules",
            task=(
                "Split monolith.py into two files: "
                "math_utils.py (containing `add` and `multiply`) and "
                "string_utils.py (containing `upper_case` and `reverse_string`). "
                "Keep the functions identical."
            ),
            setup_files={
                "monolith.py": (
                    "def add(a, b): return a + b\n"
                    "def multiply(a, b): return a * b\n"
                    "def upper_case(s): return s.upper()\n"
                    "def reverse_string(s): return s[::-1]\n"
                ),
            },
            expected_files={
                "math_utils.py": "def add",
                "string_utils.py": "def upper_case",
            },
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "split"],
        ),
        Scenario(
            name="add_shared_constants",
            description="Extract magic numbers into a shared constants module",
            task=(
                "Extract the magic number `3.14159` from geometry.py and "
                "the magic number `9.81` from physics.py into a new file "
                "constants.py. Update both files to import from constants.py."
            ),
            setup_files={
                "geometry.py": "def circle_area(r): return 3.14159 * r * r\n",
                "physics.py": "def weight(mass): return mass * 9.81\n",
            },
            expected_files={
                "constants.py": "3.14159",
                "geometry.py": "from constants import",
                "physics.py": "from constants import",
            },
            difficulty="medium",
            category="multi_file",
            tags=["refactor", "constants"],
        ),

        # ── Bug introduction / fix ───────────────────────────────────────────
        Scenario(
            name="fix_null_pointer",
            description="Fix a None-reference bug",
            task=(
                "Fix the bug in profile.py: `get_display_name` crashes with "
                "AttributeError when `user` is None. It should return 'Anonymous' instead."
            ),
            setup_files={
                "profile.py": (
                    "def get_display_name(user):\n"
                    "    return user['name'].title()\n"
                ),
            },
            expected_files={"profile.py": "Anonymous"},
            test_command=(
                "python -c \""
                "from profile import get_display_name; "
                "assert get_display_name(None) == 'Anonymous'\""
            ),
            difficulty="medium",
            category="bug_fix",
            tags=["null_check"],
        ),
        Scenario(
            name="fix_resource_leak",
            description="Fix a file-handle resource leak",
            task=(
                "Fix the resource leak in logger.py: `append_log` opens the file "
                "but never closes it. Use a context manager to ensure the file "
                "is always closed."
            ),
            setup_files={
                "logger.py": (
                    "def append_log(path, message):\n"
                    "    f = open(path, 'a')\n"
                    "    f.write(message + '\\n')\n"
                ),
            },
            expected_files={"logger.py": "with open"},
            difficulty="easy",
            category="bug_fix",
            tags=["resource_leak"],
        ),
        Scenario(
            name="fix_mutable_default_arg",
            description="Fix the mutable default argument anti-pattern",
            task=(
                "Fix the mutable default argument bug in accumulator.py: "
                "`collect` uses `items=[]` as a default argument, which causes "
                "values to persist across calls. Fix it using the `None` sentinel pattern."
            ),
            setup_files={
                "accumulator.py": (
                    "def collect(value, items=[]):\n"
                    "    items.append(value)\n"
                    "    return items\n"
                ),
            },
            expected_files={"accumulator.py": "if items is None"},
            test_command=(
                "python -c \""
                "from accumulator import collect; "
                "a = collect(1); b = collect(2); "
                "assert a != b, 'mutable default not fixed'\""
            ),
            difficulty="medium",
            category="bug_fix",
            tags=["mutable_default"],
        ),

        # ── Context boundary (requires reading multiple files) ───────────────
        Scenario(
            name="trace_call_chain",
            description="Follow a 5-file call chain to find the root cause",
            task=(
                "The function `run_pipeline` in pipeline.py is raising a KeyError. "
                "Trace through the call chain (pipeline.py → stage.py → transform.py → "
                "validator.py → schema.py) and fix the root cause."
            ),
            setup_files={
                "schema.py": "REQUIRED_FIELDS = ['name', 'value']\n",
                "validator.py": (
                    "from schema import REQUIRED_FIELDS\n\n"
                    "def validate(record):\n"
                    "    for f in REQUIRED_FIELDS:\n"
                    "        _ = record[f]  # KeyError if missing\n"
                    "    return True\n"
                ),
                "transform.py": (
                    "from validator import validate\n\n"
                    "def transform(record):\n"
                    "    validate(record)\n"
                    "    return {k: str(v) for k, v in record.items()}\n"
                ),
                "stage.py": (
                    "from transform import transform\n\n"
                    "def process_stage(records):\n"
                    "    return [transform(r) for r in records]\n"
                ),
                "pipeline.py": (
                    "from stage import process_stage\n\n"
                    "def run_pipeline(data):\n"
                    "    return process_stage(data)\n"
                ),
            },
            expected_files={"validator.py": "def validate"},
            difficulty="hard",
            category="context_boundary",
            tags=["multi_file_read", "call_chain"],
        ),
        Scenario(
            name="dependency_graph_refactor",
            description="Refactor code that spans 5 interdependent files",
            task=(
                "Add a `timeout` parameter (default=30) to `fetch_data` in fetcher.py. "
                "Propagate it through: fetcher.py → session.py → connection.py → "
                "retry.py → config.py (where the default should be defined as "
                "`DEFAULT_TIMEOUT = 30`)."
            ),
            setup_files={
                "config.py": "# Connection configuration\nMAX_RETRIES = 3\n",
                "retry.py": "def with_retries(fn, retries):\n    return fn()\n",
                "connection.py": "def connect(host): return {'host': host}\n",
                "session.py": "from connection import connect\ndef open_session(host): return connect(host)\n",
                "fetcher.py": "from session import open_session\ndef fetch_data(url): return open_session(url)\n",
            },
            expected_files={"config.py": "DEFAULT_TIMEOUT"},
            difficulty="hard",
            category="context_boundary",
            tags=["parameter_propagation"],
        ),

        # ── Plan adherence ───────────────────────────────────────────────────
        Scenario(
            name="multi_step_crud",
            description="Implement a multi-step CRUD module in order",
            task=(
                "Create a simple in-memory CRUD store in store.py. "
                "Follow this exact order: "
                "1. Define a `Store` class with an empty `_data` dict. "
                "2. Add a `create(key, value)` method. "
                "3. Add a `read(key)` method that returns None if missing. "
                "4. Add an `update(key, value)` method. "
                "5. Add a `delete(key)` method. "
                "6. Write a test file test_store.py that tests all four operations."
            ),
            expected_files={
                "store.py": "class Store",
                "test_store.py": "def test_",
            },
            test_command=(
                "python -c \""
                "from store import Store; s = Store(); "
                "s.create('k', 1); assert s.read('k') == 1; "
                "s.update('k', 2); assert s.read('k') == 2; "
                "s.delete('k'); assert s.read('k') is None\""
            ),
            difficulty="medium",
            category="plan_adherence",
            tags=["crud", "multi_step"],
        ),

        # ── Tool failure recovery ────────────────────────────────────────────
        Scenario(
            name="recover_from_syntax_error",
            description="Agent self-corrects after writing syntactically invalid Python",
            task=(
                "Create a file sorter.py with a function `sort_descending(items)` "
                "that returns the list sorted in descending order. "
                "Make sure the file is valid Python."
            ),
            expected_files={"sorter.py": "def sort_descending"},
            test_command=(
                "python -c \""
                "import ast; ast.parse(open('sorter.py').read()); "
                "from sorter import sort_descending; "
                "assert sort_descending([3,1,2]) == [3,2,1]\""
            ),
            difficulty="easy",
            category="tool_failure_recovery",
            tags=["syntax_check", "self_correction"],
        ),
        Scenario(
            name="recover_from_import_error",
            description="Agent fixes a broken import after creating it",
            task=(
                "Create main.py that imports and uses `greet` from greeter.py. "
                "Also create greeter.py with the `greet(name)` function that "
                "returns f'Hello, {name}!'. Ensure both files work together."
            ),
            expected_files={
                "greeter.py": "def greet",
                "main.py": "from greeter import greet",
            },
            test_command="python -c \"import main\"",
            difficulty="easy",
            category="tool_failure_recovery",
            tags=["import_fix"],
        ),

        # ── Task disambiguation ──────────────────────────────────────────────
        Scenario(
            name="ambiguous_format_request",
            description="Interpret an ambiguous formatting request sensibly",
            task=(
                "Format the data in records.py. "
                "The file contains a list of dicts; make sure they are consistently "
                "formatted with one dict per line and keys in alphabetical order."
            ),
            setup_files={
                "records.py": (
                    "records = [\n"
                    "    {'z': 3, 'a': 1, 'name': 'Alice'},\n"
                    "    {'name': 'Bob', 'a': 2, 'z': 4},\n"
                    "]\n"
                ),
            },
            expected_files={"records.py": "records"},
            difficulty="medium",
            category="disambiguation",
            tags=["formatting"],
        ),

        # ── Delegation / subagent ────────────────────────────────────────────
        Scenario(
            name="delegate_test_generation",
            description="Delegate test writing to a subagent",
            task=(
                "We have a module math_ops.py. Delegate to a subagent the task of "
                "writing comprehensive pytest tests for it in test_math_ops.py. "
                "The tests should cover `add`, `subtract`, `multiply`, and `divide` "
                "(divide should raise ZeroDivisionError for divisor=0)."
            ),
            setup_files={
                "math_ops.py": (
                    "def add(a, b): return a + b\n"
                    "def subtract(a, b): return a - b\n"
                    "def multiply(a, b): return a * b\n"
                    "def divide(a, b):\n"
                    "    if b == 0:\n"
                    "        raise ZeroDivisionError('Cannot divide by zero')\n"
                    "    return a / b\n"
                ),
            },
            expected_files={"test_math_ops.py": "def test_"},
            difficulty="medium",
            category="delegation",
            tags=["subagent", "test_generation"],
        ),
        Scenario(
            name="delegate_documentation",
            description="Delegate README generation to a subagent",
            task=(
                "Delegate to a subagent the task of writing a README.md for this project. "
                "The README should include: project title, description, installation, "
                "and a usage example. Base it on the code in app.py."
            ),
            setup_files={
                "app.py": (
                    "\"\"\"Simple web scraper utility.\"\"\"\n\n"
                    "def scrape(url, selector):\n"
                    "    \"\"\"Scrape *selector* elements from *url*.\"\"\"\n"
                    "    pass\n"
                ),
            },
            expected_files={"README.md": "# "},
            difficulty="easy",
            category="delegation",
            tags=["subagent", "documentation"],
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


# ---------------------------------------------------------------------------
# G10: pass@k — unbiased estimator (Chen et al. 2021, HumanEval paper)
# ---------------------------------------------------------------------------

def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased estimator for pass@k.

    Given *n* independent samples of a scenario, *c* of which passed, return
    the probability that at least one of *k* randomly chosen samples passes.

    Formula:  pass@k = 1 - C(n-c, k) / C(n, k)

    Args:
        n: total number of attempts.
        c: number of passing attempts (c <= n).
        k: number of samples to consider (k <= n).

    Returns:
        Estimated probability in [0, 1].

    Raises:
        ValueError: if k > n or c > n or any argument is negative.
    """
    if n < 0 or c < 0 or k < 0:
        raise ValueError(f"n, c, k must be non-negative; got n={n}, c={c}, k={k}")
    if c > n:
        raise ValueError(f"c ({c}) cannot exceed n ({n})")
    if k > n:
        raise ValueError(f"k ({k}) cannot exceed n ({n})")
    if k == 0:
        return 0.0
    if n == 0:
        return 0.0
    # Use log-space to avoid overflow for large n.
    # log C(n-c, k) = log_comb(n-c, k); log C(n, k) = log_comb(n, k)
    if n - c < k:
        # Fewer failures than k — at least one pass is guaranteed.
        return 1.0
    log_numerator = math.lgamma(n - c + 1) - math.lgamma(k + 1) - math.lgamma(n - c - k + 1)
    log_denominator = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    return float(1.0 - math.exp(log_numerator - log_denominator))


def run_pass_at_k(
    scenario: "Scenario",
    agent_factory: Callable[[], Any],
    n: int,
    k: int,
    workdir: Optional[str] = None,
) -> Dict[str, Any]:
    """Run *scenario* n times and return pass@k alongside raw counts.

    Each run gets its own isolated temporary directory so side-effects from
    one attempt cannot influence the next.

    Args:
        scenario: The scenario to evaluate.
        agent_factory: Callable that returns a fresh agent for each attempt.
        n: Total number of independent attempts.
        k: k for the pass@k estimate (must be <= n).
        workdir: Optional parent directory for temporary run directories.

    Returns:
        dict with keys ``n``, ``c`` (passes), ``k``, ``pass_at_k``,
        ``results`` (list of ScenarioResult).
    """
    if k > n:
        raise ValueError(f"k ({k}) must be <= n ({n})")

    base = Path(workdir) if workdir else Path(tempfile.mkdtemp())
    results: List["ScenarioResult"] = []

    for attempt in range(n):
        attempt_dir = base / f"{scenario.name}_attempt_{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        evaluator = ScenarioEvaluator(workdir=str(attempt_dir))
        result = evaluator.run_scenario(scenario, agent_factory)
        results.append(result)

    c = sum(1 for r in results if r.status == "pass")
    return {
        "scenario": scenario.name,
        "n": n,
        "c": c,
        "k": k,
        "pass_at_k": pass_at_k(n, c, k),
        "results": results,
    }
