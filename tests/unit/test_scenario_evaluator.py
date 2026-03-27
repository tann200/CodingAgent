"""
Tests for scenario evaluation framework.

ET-2: ScenarioEvaluator CI integration — ensures full lifecycle coverage
without requiring a live LLM backend.
"""

import json
import pytest
import shutil
from datetime import datetime
from pathlib import Path

from src.core.evaluation.scenario_evaluator import (
    Scenario,
    ScenarioResult,
    ScenarioEvaluator,
    get_default_scenarios,
    run_benchmark,
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


class TestScenarioDefaults:
    """Scenario dataclass default field values."""

    def test_optional_defaults(self):
        s = Scenario(name="x", description="d", task="t")
        assert s.setup_files == {}
        assert s.expected_files == {}
        assert s.test_command is None
        assert s.cleanup_command is None
        assert s.difficulty == "medium"
        assert s.category == "general"
        assert s.tags == []


class TestAddScenariosFromFile:
    """Tests for loading scenarios from JSON."""

    def _write_json(self, tmp_path, data):
        p = tmp_path / "scenarios.json"
        p.write_text(json.dumps(data))
        return str(p)

    def test_loads_basic_scenarios(self, tmp_path):
        data = {
            "scenarios": [
                {"name": "a", "description": "desc a", "task": "do a"},
                {"name": "b", "description": "desc b", "task": "do b"},
            ]
        }
        filepath = self._write_json(tmp_path, data)
        ev = ScenarioEvaluator()
        ev.add_scenarios_from_file(filepath)
        assert len(ev.scenarios) == 2
        assert ev.scenarios[0].name == "a"
        assert ev.scenarios[1].name == "b"

    def test_loads_optional_fields(self, tmp_path):
        data = {
            "scenarios": [
                {
                    "name": "c",
                    "description": "desc",
                    "task": "do c",
                    "difficulty": "hard",
                    "category": "testing",
                    "tags": ["pytest"],
                    "test_command": "echo ok",
                    "setup_files": {"init.py": "# init"},
                    "expected_files": {"out.py": "x = 1"},
                }
            ]
        }
        filepath = self._write_json(tmp_path, data)
        ev = ScenarioEvaluator()
        ev.add_scenarios_from_file(filepath)
        s = ev.scenarios[0]
        assert s.difficulty == "hard"
        assert s.category == "testing"
        assert s.tags == ["pytest"]
        assert s.test_command == "echo ok"

    def test_empty_scenarios_list(self, tmp_path):
        filepath = self._write_json(tmp_path, {"scenarios": []})
        ev = ScenarioEvaluator()
        ev.add_scenarios_from_file(filepath)
        assert ev.scenarios == []


class TestRunScenario:
    """Tests for run_scenario with mock agents."""

    def test_pass_when_expected_file_written(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        s = Scenario(
            name="simple",
            description="create hello",
            task="Create hello.py",
            expected_files={"hello.py": "def hello():"},
        )

        def factory():
            class Agent:
                def run(self, task, working_dir=None):
                    d = Path(working_dir) if working_dir else Path(".")
                    (d / "hello.py").write_text("def hello():\n    return 'Hello World'\n")
            return Agent()

        result = ev.run_scenario(s, factory)
        assert result.status == "pass"
        assert result.scenario_name == "simple"
        assert result.duration_seconds >= 0

    def test_fail_when_expected_file_missing(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        s = Scenario(
            name="fail_scenario",
            description="desc",
            task="Create missing.py",
            expected_files={"missing.py": "# content"},
        )
        result = ev.run_scenario(s, lambda: None)
        assert result.status == "fail"

    def test_fail_when_file_content_wrong(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        s = Scenario(
            name="wrong_content",
            description="desc",
            task="Create output.py",
            expected_files={"output.py": "expected_content_xyz"},
        )

        def factory():
            class Agent:
                def run(self, task, working_dir=None):
                    d = Path(working_dir) if working_dir else Path(".")
                    (d / "output.py").write_text("wrong content\n")
            return Agent()

        result = ev.run_scenario(s, factory)
        assert result.status == "fail"

    def test_agent_crash_is_handled(self, tmp_path):
        """Agent crash inside run() is caught as a warning; scenario proceeds."""
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        s = Scenario(name="crash", description="d", task="t")

        def factory():
            class Agent:
                def run(self, task, working_dir=None):
                    raise RuntimeError("agent crashed!")
            return Agent()

        result = ev.run_scenario(s, factory)
        assert result.status in ("pass", "fail", "error")

    def test_factory_crash_becomes_error(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        s = Scenario(name="factory_crash", description="d", task="t")

        def bad_factory():
            raise ValueError("factory error")

        result = ev.run_scenario(s, bad_factory)
        assert result.status == "error"
        assert "factory error" in (result.error or "")

    def test_result_timestamps_populated(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        s = Scenario(name="s", description="d", task="t")
        result = ev.run_scenario(s, lambda: None)
        assert isinstance(result.start_time, datetime)
        assert isinstance(result.end_time, datetime)
        assert result.end_time >= result.start_time


class TestRunEvaluation:
    """Tests for run_evaluation with multiple scenarios."""

    def test_returns_all_results(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        for i in range(3):
            ev.add_scenario(Scenario(name=f"s{i}", description="d", task="t"))
        results = ev.run_evaluation(lambda: None)
        assert len(results) == 3

    def test_results_stored_on_evaluator(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        ev.add_scenario(Scenario(name="x", description="d", task="t"))
        results = ev.run_evaluation(lambda: None)
        assert ev.results is results

    def test_empty_suite(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        results = ev.run_evaluation(lambda: None)
        assert results == []


class TestGetSummaryExtended:
    """Extended summary tests."""

    def _make_results(self, statuses):
        now = datetime.now()
        return [
            ScenarioResult(f"s{i}", st, now, now, 1.0)
            for i, st in enumerate(statuses)
        ]

    def test_pass_rate_calculation(self):
        ev = ScenarioEvaluator()
        results = self._make_results(["pass", "pass", "fail", "fail"])
        summary = ev.get_summary(results)
        assert summary["pass_rate"] == 0.5

    def test_duration_aggregates(self):
        ev = ScenarioEvaluator()
        results = self._make_results(["pass", "pass"])
        summary = ev.get_summary(results)
        assert summary["total_duration_seconds"] == 2.0
        assert summary["average_duration_seconds"] == 1.0

    def test_uses_stored_results_by_default(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        ev.add_scenario(Scenario(name="x", description="d", task="t"))
        ev.run_evaluation(lambda: None)
        summary = ev.get_summary()
        assert summary["total"] == 1


class TestGetSummaryByCategory:
    """Tests for category/difficulty breakdown."""

    def test_by_category_breakdown(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        ev.add_scenario(Scenario(name="a", description="d", task="t", category="bugfix"))
        ev.add_scenario(Scenario(name="b", description="d", task="t", category="feature"))
        ev.add_scenario(Scenario(name="c", description="d", task="t", category="bugfix"))
        ev.run_evaluation(lambda: None)
        breakdown = ev.get_summary_by_category()
        assert "by_category" in breakdown
        assert "by_difficulty" in breakdown
        assert "bugfix" in breakdown["by_category"]
        assert "feature" in breakdown["by_category"]

    def test_by_difficulty_breakdown(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        ev.add_scenario(Scenario(name="easy1", description="d", task="t", difficulty="easy"))
        ev.add_scenario(Scenario(name="hard1", description="d", task="t", difficulty="hard"))
        ev.run_evaluation(lambda: None)
        breakdown = ev.get_summary_by_category()
        assert "easy" in breakdown["by_difficulty"]
        assert "hard" in breakdown["by_difficulty"]


class TestExportResults:
    """Tests for JSON result export."""

    def test_exports_valid_json(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        ev.add_scenario(Scenario(name="exp", description="d", task="t"))
        ev.run_evaluation(lambda: None)

        out = str(tmp_path / "results.json")
        ev.export_results(out)

        with open(out) as f:
            data = json.load(f)
        assert "summary" in data
        assert "results" in data
        assert len(data["results"]) == 1

    def test_export_contains_scenario_names(self, tmp_path):
        ev = ScenarioEvaluator(workdir=str(tmp_path))
        ev.add_scenario(Scenario(name="named_scenario", description="d", task="t"))
        ev.run_evaluation(lambda: None)

        out = str(tmp_path / "out.json")
        ev.export_results(out)
        with open(out) as f:
            data = json.load(f)
        assert data["results"][0]["scenario_name"] == "named_scenario"


class TestRunBenchmark:
    """Tests for run_benchmark helper."""

    def test_run_benchmark_with_custom_scenarios(self, tmp_path):
        scenarios = [
            Scenario(name="bench1", description="d", task="t"),
            Scenario(name="bench2", description="d", task="t"),
        ]
        summary = run_benchmark(lambda: None, scenarios=scenarios)
        assert "total" in summary
        assert summary["total"] == 2

    def test_run_benchmark_uses_defaults_when_none(self):
        summary = run_benchmark(lambda: None)
        assert "total" in summary
        assert summary["total"] == len(get_default_scenarios())
