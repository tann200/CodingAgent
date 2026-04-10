"""tests/integration/test_scenario_smoke.py — ET-2

Smoke tests for ScenarioEvaluator that always run in CI without a live LLM.
These tests exercise setup, verification, and framework internals only.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

from src.core.evaluation.scenario_evaluator import (
    Scenario,
    ScenarioEvaluator,
    get_default_scenarios,
    run_benchmark,
)


class TestScenarioEvaluatorSetupAndVerify:
    """ScenarioEvaluator._setup_scenario and _verify_scenario work without an agent."""

    def test_setup_creates_files(self, tmp_path):
        """_setup_scenario writes setup_files to disk."""
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(
            name="setup_test",
            description="Check setup creates files",
            task="",
            setup_files={"hello.py": "def hello():\n    return 'Hello World'\n"},
        )
        scenario_dir = evaluator._setup_scenario(scenario)
        assert (scenario_dir / "hello.py").exists()
        assert "def hello" in (scenario_dir / "hello.py").read_text()

    def test_verify_passes_when_expected_content_present(self, tmp_path):
        """_verify_scenario returns True when expected file contains expected content."""
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(
            name="verify_pass",
            description="File with correct content",
            task="",
            setup_files={"hello.py": "def hello():\n    return 'Hello World'\n"},
            expected_files={"hello.py": "def hello():"},
        )
        scenario_dir = evaluator._setup_scenario(scenario)
        passed, output = evaluator._verify_scenario(scenario, scenario_dir)
        assert passed

    def test_verify_fails_when_expected_file_missing(self, tmp_path):
        """_verify_scenario returns False when an expected file is absent."""
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(
            name="verify_fail_missing",
            description="Expected file not created",
            task="",
            expected_files={"missing.py": "def foo():"},
        )
        scenario_dir = evaluator._setup_scenario(scenario)
        passed, output = evaluator._verify_scenario(scenario, scenario_dir)
        assert not passed
        assert "missing.py" in output

    def test_verify_fails_when_content_mismatch(self, tmp_path):
        """_verify_scenario returns False when file content does not match."""
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(
            name="verify_fail_content",
            description="File has wrong content",
            task="",
            setup_files={"hello.py": "def wrong():\n    pass\n"},
            expected_files={"hello.py": "def hello():"},
        )
        scenario_dir = evaluator._setup_scenario(scenario)
        passed, output = evaluator._verify_scenario(scenario, scenario_dir)
        assert not passed

    def test_verify_passes_with_no_expected_files(self, tmp_path):
        """_verify_scenario passes vacuously when expected_files is empty."""
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(
            name="vacuous_pass",
            description="No expectations",
            task="",
        )
        scenario_dir = evaluator._setup_scenario(scenario)
        passed, output = evaluator._verify_scenario(scenario, scenario_dir)
        assert passed


class TestRunScenarioWithNoopAgent:
    """ScenarioEvaluator.run_scenario completes even when agent does nothing."""

    def test_noop_agent_no_expected_files(self, tmp_path):
        """run_scenario passes when agent_factory returns None and no expected_files."""
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(
            name="noop_agent",
            description="Agent does nothing; no expected files",
            task="do nothing",
        )
        result = evaluator.run_scenario(scenario, agent_factory=lambda: None)
        assert result.status == "pass"
        assert result.scenario_name == "noop_agent"
        assert result.duration_seconds >= 0

    def test_noop_agent_with_expected_files_fails(self, tmp_path):
        """run_scenario fails when expected_files not created by noop agent."""
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(
            name="noop_agent_fail",
            description="Noop agent; file expected",
            task="create hello.py",
            expected_files={"hello.py": "def hello():"},
        )
        result = evaluator.run_scenario(scenario, agent_factory=lambda: None)
        assert result.status == "fail"

    def test_pre_seeded_files_pass_verify(self, tmp_path):
        """When setup_files seeds the expected content, verify passes even with noop agent."""
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(
            name="pre_seeded",
            description="File seeded in setup; noop agent; verify passes",
            task="",
            setup_files={"calc.py": "class Calculator:\n    pass\n"},
            expected_files={"calc.py": "class Calculator:"},
        )
        result = evaluator.run_scenario(scenario, agent_factory=lambda: None)
        assert result.status == "pass"


class TestGetSummary:
    """get_summary and get_summary_by_category produce correct aggregates."""

    def test_get_summary_empty(self, tmp_path):
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        summary = evaluator.get_summary([])
        assert summary["total"] == 0
        assert summary["passed"] == 0
        assert summary["pass_rate"] == 0

    def test_get_summary_all_pass(self, tmp_path):
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        # Run 2 vacuous-pass scenarios
        for i in range(2):
            scenario = Scenario(name=f"s{i}", description="", task="")
            evaluator.add_scenario(scenario)
        results = evaluator.run_evaluation(agent_factory=lambda: None)
        summary = evaluator.get_summary(results)
        assert summary["total"] == 2
        assert summary["passed"] == 2
        assert summary["pass_rate"] == 1.0

    def test_get_summary_by_category(self, tmp_path):
        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        s1 = Scenario(
            name="c1", description="", task="", category="tools", difficulty="easy"
        )
        s2 = Scenario(
            name="c2", description="", task="", category="tools", difficulty="hard"
        )
        evaluator.add_scenario(s1)
        evaluator.add_scenario(s2)
        results = evaluator.run_evaluation(agent_factory=lambda: None)
        by_cat = evaluator.get_summary_by_category(results)
        assert "tools" in by_cat["by_category"]
        assert by_cat["by_category"]["tools"]["pass"] == 2


class TestGetDefaultScenarios:
    """get_default_scenarios() returns valid Scenario objects."""

    def test_default_scenarios_non_empty(self):
        scenarios = get_default_scenarios()
        assert len(scenarios) >= 3

    def test_default_scenarios_have_required_fields(self):
        for s in get_default_scenarios():
            assert s.name, f"Scenario missing name: {s}"
            assert s.task, f"Scenario missing task: {s.name}"
            assert s.description, f"Scenario missing description: {s.name}"


class TestExportResults:
    """export_results writes valid JSON."""

    def test_export_results(self, tmp_path):
        import json

        evaluator = ScenarioEvaluator(workdir=str(tmp_path))
        scenario = Scenario(name="export_test", description="", task="")
        evaluator.add_scenario(scenario)
        evaluator.run_evaluation(agent_factory=lambda: None)

        out_path = str(tmp_path / "results.json")
        evaluator.export_results(out_path)

        with open(out_path) as f:
            data = json.load(f)
        assert "summary" in data
        assert "results" in data
        assert len(data["results"]) == 1
        assert data["results"][0]["scenario_name"] == "export_test"
