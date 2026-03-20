"""
Tests for scenario evaluation framework.
"""

import pytest
import shutil
from pathlib import Path

from src.core.evaluation.scenario_evaluator import (
    Scenario,
    ScenarioResult,
    ScenarioEvaluator,
    get_default_scenarios,
)


class TestScenario:
    """Tests for Scenario dataclass."""

    def test_scenario_creation(self):
        """Test creating a scenario."""
        scenario = Scenario(
            name="test_scenario",
            description="A test scenario",
            task="Do something",
        )

        assert scenario.name == "test_scenario"
        assert scenario.description == "A test scenario"
        assert scenario.task == "Do something"

    def test_scenario_with_files(self):
        """Test scenario with setup and expected files."""
        scenario = Scenario(
            name="file_scenario",
            description="A file scenario",
            task="Create a file",
            setup_files={"input.txt": "hello"},
            expected_files={"output.txt": "hello"},
        )

        assert scenario.setup_files["input.txt"] == "hello"
        assert scenario.expected_files["output.txt"] == "hello"


class TestScenarioEvaluator:
    """Tests for ScenarioEvaluator."""

    @pytest.fixture
    def temp_workdir(self, tmp_path):
        """Create a temporary workdir."""
        workdir = tmp_path / "eval_test"
        workdir.mkdir()
        yield str(workdir)
        shutil.rmtree(workdir, ignore_errors=True)

    def test_evaluator_initialization(self, temp_workdir):
        """Test evaluator initializes correctly."""
        evaluator = ScenarioEvaluator(temp_workdir)

        assert evaluator.workdir == Path(temp_workdir)
        assert len(evaluator.scenarios) == 0

    def test_add_scenario(self, temp_workdir):
        """Test adding a scenario."""
        evaluator = ScenarioEvaluator(temp_workdir)

        scenario = Scenario(
            name="test",
            description="test",
            task="test task",
        )
        evaluator.add_scenario(scenario)

        assert len(evaluator.scenarios) == 1

    def test_setup_scenario(self, temp_workdir):
        """Test scenario setup creates files."""
        evaluator = ScenarioEvaluator(temp_workdir)

        scenario = Scenario(
            name="setup_test",
            description="test",
            task="test",
            setup_files={
                "input.txt": "hello world",
                "config.json": '{"key": "value"}',
            },
        )

        scenario_dir = evaluator._setup_scenario(scenario)

        assert scenario_dir.exists()
        assert (scenario_dir / "input.txt").read_text() == "hello world"

    def test_verify_scenario_files(self, temp_workdir):
        """Test scenario verification checks files."""
        evaluator = ScenarioEvaluator(temp_workdir)

        scenario = Scenario(
            name="verify_test",
            description="test",
            task="test",
            expected_files={
                "expected.txt": "expected content",
            },
        )

        scenario_dir = evaluator._setup_scenario(scenario)
        (scenario_dir / "expected.txt").write_text("expected content")

        passed, output = evaluator._verify_scenario(scenario, scenario_dir)

        assert passed is True

    def test_verify_scenario_missing_file(self, temp_workdir):
        """Test verification fails for missing file."""
        evaluator = ScenarioEvaluator(temp_workdir)

        scenario = Scenario(
            name="verify_fail",
            description="test",
            task="test",
            expected_files={"missing.txt": "content"},
        )

        scenario_dir = evaluator._setup_scenario(scenario)

        passed, output = evaluator._verify_scenario(scenario, scenario_dir)

        assert passed is False
        assert "not found" in output

    def test_verify_test_command(self, temp_workdir):
        """Test running test command for verification."""
        evaluator = ScenarioEvaluator(temp_workdir)

        scenario = Scenario(
            name="command_test",
            description="test",
            task="test",
            test_command="echo 'test passed'",
        )

        scenario_dir = evaluator._setup_scenario(scenario)

        passed, output = evaluator._verify_scenario(scenario, scenario_dir)

        assert passed is True
        assert "test passed" in output

    def test_get_summary_empty(self, temp_workdir):
        """Test summary with no results."""
        evaluator = ScenarioEvaluator(temp_workdir)

        summary = evaluator.get_summary()

        assert summary["total"] == 0
        assert summary["pass_rate"] == 0

    def test_get_summary_with_results(self, temp_workdir):
        """Test summary with results."""
        evaluator = ScenarioEvaluator(temp_workdir)

        results = [
            ScenarioResult("test1", "pass", None, None, 1.0),
            ScenarioResult("test2", "fail", None, None, 2.0),
            ScenarioResult("test3", "error", None, None, 3.0),
        ]

        summary = evaluator.get_summary(results)

        assert summary["total"] == 3
        assert summary["passed"] == 1
        assert summary["failed"] == 1
        assert summary["errors"] == 1


class TestGetDefaultScenarios:
    """Tests for default scenarios."""

    def test_get_default_scenarios(self):
        """Test getting default scenarios."""
        scenarios = get_default_scenarios()

        assert len(scenarios) > 0
        assert all(isinstance(s, Scenario) for s in scenarios)

    def test_default_scenario_has_required_fields(self):
        """Test default scenarios have required fields."""
        scenarios = get_default_scenarios()

        for scenario in scenarios:
            assert scenario.name
            assert scenario.task
