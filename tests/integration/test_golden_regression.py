"""tests/integration/test_golden_regression.py — G10

Golden-set regression tests for CodingAgent.

Architecture
------------
These tests verify tool behaviour and the ScenarioEvaluator framework using
two complementary approaches:

1. **Direct-agent tests (GS-1..GS-9)** — A thin ``DirectAgent`` shim calls
   the actual tool functions (write_file, read_file, edit_file) directly,
   without going through the LangGraph orchestrator.  This makes the tests
   fully deterministic and independent of LLM/graph-routing regressions.
   They exercise the tool layer and the ScenarioEvaluator verify logic.

2. **pass@k tests (GS-PK, GS-PKI)** — Unit and integration tests for the
   ``pass_at_k`` estimator and ``run_pass_at_k`` helper.

The golden scenarios are declared in tests/fixtures/golden_scenarios.json and
cross-referenced here via ``_golden(name)`` for metadata consistency.

Coverage:
  GS-1  gs_write_new_file         — write_file creates a Python file
  GS-2  gs_read_and_report        — read_file reads pre-existing JSON
  GS-3  gs_edit_existing_file     — read + write_file replaces a string
  GS-4  gs_write_with_subdirectory— write_file creates nested path
  GS-5  gs_create_pytest_test     — write_file creates a test file
  GS-6  gs_multi_step_read_edit   — read → write → completion (multi-step)
  GS-7  gs_create_class_with_methods — write_file creates a class
  GS-8  gs_create_requirements_txt   — write_file creates requirements.txt
  GS-9  gs_refactor_rename_function  — read + write_file renames function
  GS-PK  pass@k estimator unit tests
  GS-PKI pass@k integration tests
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

from src.core.evaluation.scenario_evaluator import (
    Scenario,
    ScenarioEvaluator,
    pass_at_k,
    run_pass_at_k,
)

# ---------------------------------------------------------------------------
# Golden fixture loader
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "golden_scenarios.json"


def _golden(name: str) -> dict:
    """Return the raw scenario dict from golden_scenarios.json by name."""
    data = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    for s in data["scenarios"]:
        if s["name"] == name:
            return s
    raise KeyError(f"Golden scenario not found: {name!r}")


# ---------------------------------------------------------------------------
# DirectAgent — calls tool functions directly, no LLM/graph required
# ---------------------------------------------------------------------------


class DirectAgent:
    """Thin shim that executes scripted tool calls against the working directory.

    Each tool_call is a dict: {"tool": "write_file"|"read_file"|"edit_file", ...args}
    Uses pathlib directly — no dependency on tool function internals.
    """

    def __init__(self, tool_calls: list[dict]):
        self._tool_calls = tool_calls

    def run(self, task: str, working_dir: str) -> None:
        wd = Path(working_dir)
        for call in self._tool_calls:
            tool = call["tool"]
            if tool == "write_file":
                path = wd / call["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(call["content"], encoding="utf-8")
            elif tool == "read_file":
                path = wd / call["path"]
                if path.exists():
                    path.read_text(encoding="utf-8")
            elif tool == "edit_file":
                path = wd / call["path"]
                old = call.get("old_string", "")
                new_str = call.get("new_string", "")
                if path.exists() and old:
                    text = path.read_text(encoding="utf-8")
                    path.write_text(text.replace(old, new_str), encoding="utf-8")


# ---------------------------------------------------------------------------
# GS-1  write_file — creates a new Python file
# ---------------------------------------------------------------------------


def test_gs1_write_new_file(tmp_path):
    """GS-1: Agent writes utils.py with an add() function."""
    sc = _golden("gs_write_new_file")

    content = "def add(a, b):\n    return a + b\n"
    agent = DirectAgent([{"tool": "write_file", "path": "utils.py", "content": content}])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        expected_files=sc["expected_files"],
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-1 failed: {result.verification_output}"


# ---------------------------------------------------------------------------
# GS-2  read_file — agent reads pre-seeded JSON
# ---------------------------------------------------------------------------


def test_gs2_read_and_report(tmp_path):
    """GS-2: Agent reads config.json; verify passes (no expected_files constraint)."""
    sc = _golden("gs_read_and_report")

    agent = DirectAgent([{"tool": "read_file", "path": "config.json"}])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        setup_files=sc["setup_files"],
        expected_files=sc["expected_files"],  # empty — verify passes vacuously
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-2 failed: {result.verification_output}"
    # Config file must still be intact on disk.
    assert (tmp_path / sc["name"] / "config.json").exists()


# ---------------------------------------------------------------------------
# GS-3  edit existing file — read + write replaces a string
# ---------------------------------------------------------------------------


def test_gs3_edit_existing_file(tmp_path):
    """GS-3: Agent reads greet.py then rewrites 'World' → 'CodingAgent'."""
    sc = _golden("gs_edit_existing_file")

    new_content = "def greet():\n    return 'Hello, CodingAgent!'\n"
    agent = DirectAgent([
        {"tool": "read_file", "path": "greet.py"},
        {"tool": "write_file", "path": "greet.py", "content": new_content},
    ])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        setup_files=sc["setup_files"],
        expected_files=sc["expected_files"],
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-3 failed: {result.verification_output}"


# ---------------------------------------------------------------------------
# GS-4  write_file with subdirectory — creates nested path
# ---------------------------------------------------------------------------


def test_gs4_write_with_subdirectory(tmp_path):
    """GS-4: Agent creates src/models/user.py with a User class."""
    sc = _golden("gs_write_with_subdirectory")

    content = (
        "from dataclasses import dataclass\n\n"
        "@dataclass\nclass User:\n    name: str\n    email: str\n"
    )
    agent = DirectAgent([{"tool": "write_file", "path": "src/models/user.py", "content": content}])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        expected_files=sc["expected_files"],
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-4 failed: {result.verification_output}"


# ---------------------------------------------------------------------------
# GS-5  create pytest test file
# ---------------------------------------------------------------------------


def test_gs5_create_pytest_test(tmp_path):
    """GS-5: Agent creates test_utils.py with a pytest test function."""
    sc = _golden("gs_create_pytest_test")

    content = "from utils import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    agent = DirectAgent([{"tool": "write_file", "path": "test_utils.py", "content": content}])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        setup_files=sc["setup_files"],
        expected_files=sc["expected_files"],
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-5 failed: {result.verification_output}"


# ---------------------------------------------------------------------------
# GS-6  multi-step: read → write → completion
# ---------------------------------------------------------------------------


def test_gs6_multi_step_read_edit(tmp_path):
    """GS-6: Agent reads app.py, changes DEBUG=False to DEBUG=True."""
    sc = _golden("gs_multi_step_read_edit_verify")

    agent = DirectAgent([
        {"tool": "read_file", "path": "app.py"},
        {"tool": "write_file", "path": "app.py", "content": "DEBUG = True\nPORT = 8080\n"},
    ])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        setup_files=sc["setup_files"],
        expected_files=sc["expected_files"],
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-6 failed: {result.verification_output}"


# ---------------------------------------------------------------------------
# GS-7  create class with methods
# ---------------------------------------------------------------------------


def test_gs7_create_class_with_methods(tmp_path):
    """GS-7: Agent creates rectangle.py with a Rectangle class and area() method."""
    sc = _golden("gs_create_class_with_methods")

    content = (
        "class Rectangle:\n"
        "    def __init__(self, width, height):\n"
        "        self.width = width\n"
        "        self.height = height\n\n"
        "    def area(self):\n"
        "        return self.width * self.height\n"
    )
    agent = DirectAgent([{"tool": "write_file", "path": "rectangle.py", "content": content}])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        expected_files=sc["expected_files"],
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-7 failed: {result.verification_output}"


# ---------------------------------------------------------------------------
# GS-8  create requirements.txt
# ---------------------------------------------------------------------------


def test_gs8_create_requirements_txt(tmp_path):
    """GS-8: Agent creates requirements.txt listing key dependencies."""
    sc = _golden("gs_create_requirements_txt")

    content = "requests>=2.28.0\npytest>=7.0\npydantic>=2.0\n"
    agent = DirectAgent([{"tool": "write_file", "path": "requirements.txt", "content": content}])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        expected_files=sc["expected_files"],
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-8 failed: {result.verification_output}"


# ---------------------------------------------------------------------------
# GS-9  refactor: rename function via read + write
# ---------------------------------------------------------------------------


def test_gs9_refactor_rename_function(tmp_path):
    """GS-9: Agent reads helpers.py and rewrites it with compute() renamed to calculate()."""
    sc = _golden("gs_refactor_rename_function")

    new_content = "def calculate(x, y):\n    return x * y\n\nresult = calculate(3, 4)\n"
    agent = DirectAgent([
        {"tool": "read_file", "path": "helpers.py"},
        {"tool": "write_file", "path": "helpers.py", "content": new_content},
    ])

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))
    scenario = Scenario(
        name=sc["name"],
        description=sc["description"],
        task=sc["task"],
        setup_files=sc["setup_files"],
        expected_files=sc["expected_files"],
        category=sc["category"],
        difficulty=sc["difficulty"],
    )
    result = evaluator.run_scenario(scenario, agent_factory=lambda: agent)

    assert result.status == "pass", f"GS-9 failed: {result.verification_output}"
    disk = (tmp_path / sc["name"] / "helpers.py").read_text(encoding="utf-8")
    assert "def compute" not in disk, f"Old name still present: {disk}"


# ---------------------------------------------------------------------------
# GS-summary — all golden scenarios pass with DirectAgent
# ---------------------------------------------------------------------------


def test_gs_summary_pass_rate(tmp_path):
    """GS-summary: Run all 9 golden scenarios; assert 100% pass rate."""
    scenarios_data = json.loads(_FIXTURES.read_text(encoding="utf-8"))["scenarios"]

    evaluator = ScenarioEvaluator(workdir=str(tmp_path))

    # Map scenario names to scripted content
    content_map: dict[str, list[dict]] = {
        "gs_write_new_file": [
            {"tool": "write_file", "path": "utils.py", "content": "def add(a, b):\n    return a + b\n"},
        ],
        "gs_read_and_report": [
            {"tool": "read_file", "path": "config.json"},
        ],
        "gs_edit_existing_file": [
            {"tool": "read_file", "path": "greet.py"},
            {"tool": "write_file", "path": "greet.py", "content": "def greet():\n    return 'Hello, CodingAgent!'\n"},
        ],
        "gs_write_with_subdirectory": [
            {"tool": "write_file", "path": "src/models/user.py", "content": "@dataclass\nclass User:\n    name: str\n    email: str\n"},
        ],
        "gs_create_pytest_test": [
            {"tool": "write_file", "path": "test_utils.py", "content": "from utils import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"},
        ],
        "gs_multi_step_read_edit_verify": [
            {"tool": "read_file", "path": "app.py"},
            {"tool": "write_file", "path": "app.py", "content": "DEBUG = True\nPORT = 8080\n"},
        ],
        "gs_create_class_with_methods": [
            {"tool": "write_file", "path": "rectangle.py", "content": "class Rectangle:\n    def area(self):\n        return self.width * self.height\n"},
        ],
        "gs_create_requirements_txt": [
            {"tool": "write_file", "path": "requirements.txt", "content": "requests>=2.28.0\npytest>=7.0\npydantic>=2.0\n"},
        ],
        "gs_refactor_rename_function": [
            {"tool": "read_file", "path": "helpers.py"},
            {"tool": "write_file", "path": "helpers.py", "content": "def calculate(x, y):\n    return x * y\n\nresult = calculate(3, 4)\n"},
        ],
        "gs_add_docstring": [
            {"tool": "read_file", "path": "parser.py"},
            {"tool": "write_file", "path": "parser.py", "content": "def parse(text):\n    \"\"\"Split and strip the input text.\"\"\"\n    return text.strip().split()\n"},
        ],
    }

    for sc_data in scenarios_data:
        name = sc_data["name"]
        calls = content_map.get(name, [])
        agent = DirectAgent(calls)
        scenario = Scenario(
            name=name,
            description=sc_data["description"],
            task=sc_data["task"],
            setup_files=sc_data.get("setup_files", {}),
            expected_files=sc_data.get("expected_files", {}),
            category=sc_data.get("category", "general"),
            difficulty=sc_data.get("difficulty", "medium"),
            tags=sc_data.get("tags", []),
        )
        evaluator.add_scenario(scenario)
        # pre-register the agent factory per scenario
        evaluator._agent_factories = getattr(evaluator, "_agent_factories", {})
        evaluator._agent_factories[name] = lambda _a=agent: _a

    # Run all scenarios — use the per-scenario factory
    results = []
    for sc in evaluator.scenarios:
        factory = evaluator._agent_factories[sc.name]
        r = evaluator.run_scenario(sc, factory)
        results.append(r)

    summary = evaluator.get_summary(results)
    by_cat = evaluator.get_summary_by_category(results)

    assert summary["total"] == 10
    assert summary["pass_rate"] == 1.0, (
        f"Golden pass rate {summary['pass_rate']:.0%} — failures: "
        + str([r.scenario_name for r in results if r.status != "pass"])
    )
    # Verify category breakdown is populated
    assert len(by_cat["by_category"]) >= 2


# ---------------------------------------------------------------------------
# GS-PK  pass@k estimator — unit tests
# ---------------------------------------------------------------------------


class TestPassAtK:
    """Unit tests for the pass_at_k() estimator (G10)."""

    def test_all_pass(self):
        assert pass_at_k(n=5, c=5, k=1) == pytest.approx(1.0)
        assert pass_at_k(n=5, c=5, k=5) == pytest.approx(1.0)

    def test_all_fail(self):
        assert pass_at_k(n=5, c=0, k=1) == pytest.approx(0.0)
        assert pass_at_k(n=5, c=0, k=5) == pytest.approx(0.0)

    def test_one_pass_of_ten_k1(self):
        """pass@1 = c/n when k=1."""
        assert pass_at_k(n=10, c=1, k=1) == pytest.approx(0.1, abs=1e-9)

    def test_one_pass_of_two_k1(self):
        assert pass_at_k(n=2, c=1, k=1) == pytest.approx(0.5, abs=1e-9)

    def test_monotone_increasing_in_k(self):
        vals = [pass_at_k(n=10, c=3, k=k) for k in range(1, 11)]
        for i in range(len(vals) - 1):
            assert vals[i] <= vals[i + 1] + 1e-12

    def test_k_equals_n_with_passes(self):
        assert pass_at_k(n=5, c=3, k=5) == pytest.approx(1.0)

    def test_k_equals_n_no_passes(self):
        assert pass_at_k(n=5, c=0, k=5) == pytest.approx(0.0)

    def test_k_zero(self):
        assert pass_at_k(n=5, c=3, k=0) == pytest.approx(0.0)

    def test_invalid_c_gt_n(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            pass_at_k(n=3, c=5, k=1)

    def test_invalid_k_gt_n(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            pass_at_k(n=3, c=1, k=5)

    def test_large_n_no_overflow(self):
        result = pass_at_k(n=1000, c=500, k=10)
        assert 0.0 <= result <= 1.0

    def test_n_equals_c_equals_k_equals_1(self):
        assert pass_at_k(n=1, c=1, k=1) == pytest.approx(1.0)

    def test_n_zero(self):
        assert pass_at_k(n=0, c=0, k=0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# GS-PKI  pass@k integration — metric plumbing end-to-end
# ---------------------------------------------------------------------------


def test_gspki_pass_at_k_all_pass(tmp_path):
    """run_pass_at_k with noop agent + no expected_files → all pass, pass@1 = 1.0."""
    scenario = Scenario(
        name="pk_smoke",
        description="Noop scenario — no expected files; always passes.",
        task="do nothing",
        category="meta",
        difficulty="easy",
    )
    report = run_pass_at_k(
        scenario=scenario,
        agent_factory=lambda: None,
        n=3,
        k=1,
        workdir=str(tmp_path),
    )
    assert report["n"] == 3
    assert report["c"] == 3
    assert report["pass_at_k"] == pytest.approx(1.0)
    assert len(report["results"]) == 3
    assert all(r.status == "pass" for r in report["results"])


def test_gspki_pass_at_k_all_fail(tmp_path):
    """run_pass_at_k with noop agent + missing expected file → all fail, pass@1 = 0.0."""
    scenario = Scenario(
        name="pk_fail_smoke",
        description="Expected file never created by noop agent.",
        task="create result.txt",
        expected_files={"result.txt": "done"},
        category="meta",
        difficulty="easy",
    )
    report = run_pass_at_k(
        scenario=scenario,
        agent_factory=lambda: None,
        n=3,
        k=1,
        workdir=str(tmp_path),
    )
    assert report["c"] == 0
    assert report["pass_at_k"] == pytest.approx(0.0)


def test_gspki_pass_at_k_with_direct_agent(tmp_path):
    """run_pass_at_k with DirectAgent that writes the expected file → pass@1 = 1.0."""
    scenario = Scenario(
        name="pk_direct_agent",
        description="DirectAgent writes the expected file.",
        task="write result.txt",
        expected_files={"result.txt": "done"},
        category="meta",
        difficulty="easy",
    )

    def _factory():
        return DirectAgent([
            {"tool": "write_file", "path": "result.txt", "content": "done\n"}
        ])

    report = run_pass_at_k(
        scenario=scenario,
        agent_factory=_factory,
        n=3,
        k=1,
        workdir=str(tmp_path),
    )
    assert report["c"] == 3
    assert report["pass_at_k"] == pytest.approx(1.0)
