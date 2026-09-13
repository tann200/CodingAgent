"""Audit PHASE-3 item 3.2: evaluation framework (package API, CLI, regression)."""

from __future__ import annotations

import argparse
import json
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from src.core.evaluation import regression
from src.core.evaluation import scenario_evaluator as se
from src.core.evaluation import cli


# ---------------------------------------------------------------------------
# package API
# ---------------------------------------------------------------------------


def test_package_exports() -> None:
    import src.core.evaluation as ev

    for name in (
        "Scenario",
        "ScenarioEvaluator",
        "ScenarioResult",
        "get_default_scenarios",
        "pass_at_k",
        "run_benchmark",
        "run_pass_at_k",
    ):
        assert hasattr(ev, name), name
        assert name in ev.__all__


def test_default_scenarios_count_and_required_fields() -> None:
    scenarios = se.get_default_scenarios()
    assert len(scenarios) >= 20
    for s in scenarios:
        assert s.name
        assert s.category
        assert s.difficulty in ("easy", "medium", "hard")
        assert s.task


# ---------------------------------------------------------------------------
# regression helpers
# ---------------------------------------------------------------------------


def _result(name: str, status: str, duration: float = 1.0):
    now = datetime.now()
    return se.ScenarioResult(
        scenario_name=name,
        status=status,
        start_time=now,
        end_time=now,
        duration_seconds=duration,
    )


def test_save_and_load_baseline(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "baseline.json"
    results = [_result("a", "pass"), _result("b", "fail")]
    regression.save_baseline(p, results, summary={"pass_rate": 0.5})
    doc = regression.load_baseline(p)
    assert doc is not None
    assert doc["scenarios"]["a"]["status"] == "pass"
    assert doc["scenarios"]["b"]["status"] == "fail"
    assert doc["summary"]["pass_rate"] == 0.5
    assert doc["format_version"] == 1


def test_load_baseline_missing_and_corrupt(tmp_path: Path) -> None:
    assert regression.load_baseline(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert regression.load_baseline(bad) is None
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert regression.load_baseline(wrong) is None


def test_compare_detects_regressions() -> None:
    baseline = {"scenarios": {"a": {"status": "pass"}, "b": {"status": "pass"}, "c": {"status": "fail"}}}
    results = [_result("a", "pass"), _result("b", "fail"), _result("c", "pass"), _result("d", "fail")]
    cmp = regression.compare_baseline(baseline, results)
    assert cmp["regressed"] == ["b"]
    assert cmp["improved"] == ["c"]
    assert cmp["new"] == ["d"]
    assert cmp["missing"] == []
    assert cmp["got_regressions"] is True


def test_compare_pass_to_error_is_regression() -> None:
    baseline = {"scenarios": {"a": {"status": "pass"}}}
    cmp = regression.compare_baseline(baseline, [_result("a", "error")])
    assert cmp["regressed"] == ["a"]
    assert regression.is_regression(cmp) is True


def test_compare_no_regression() -> None:
    baseline = {"scenarios": {"a": {"status": "pass"}, "b": {"status": "fail"}}}
    cmp = regression.compare_baseline(
        baseline, [_result("a", "pass"), _result("b", "fail")]
    )
    assert cmp["got_regressions"] is False
    assert regression.is_regression(cmp) is False


# ---------------------------------------------------------------------------
# CLI: list
# ---------------------------------------------------------------------------


class TestCliList:
    def test_list_prints_scenarios(self, capsys: pytest.CaptureFixture) -> None:
        rc = cli.main(["list"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "simple_function" in out
        assert "scenarios" in out

    def test_list_filters_difficulty(self, capsys: pytest.CaptureFixture) -> None:
        rc = cli.main(["list", "--difficulty", "hard"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "hard" in out
        assert "simple_function" not in out


# ---------------------------------------------------------------------------
# CLI: run + baseline-save against a fake agent
# ---------------------------------------------------------------------------


def _register_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


@pytest.fixture
def fake_agent_module(monkeypatch: pytest.MonkeyPatch) -> str:
    module = _register_module("_fake_eval_agent")

    def make_agent() -> object:
        class _Agent:
            def run(self, task: str, working_dir: "str | None" = None) -> None:
                assert working_dir is not None
                (Path(working_dir) / "hello.py").write_text(
                    "def hello():\n    return 'Hello World'\n"
                )

        return _Agent()

    setattr(module, "make_agent", make_agent)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return "_fake_eval_agent:make_agent"


def test_run_single_scenario_passes(fake_agent_module: str, capsys: pytest.CaptureFixture) -> None:
    rc = cli.main(
        ["run", "--agent", fake_agent_module, "--scenarios", "simple_function"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "passed  : 1" in out
    assert "pass rate: 100.0%" in out


def test_run_no_match_exits_2(fake_agent_module: str) -> None:
    rc = cli.main(["run", "--agent", fake_agent_module, "--scenarios", "does_not_exist"])
    assert rc == 2


def test_run_with_output_writes_report(
    fake_agent_module: str, tmp_path: Path
) -> None:
    report = tmp_path / "report.json"
    rc = cli.main(
        [
            "run",
            "--agent",
            fake_agent_module,
            "--scenarios",
            "simple_function",
            "--output",
            str(report),
        ]
    )
    assert rc == 0
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["summary"]["passed"] == 1
    assert doc["results"][0]["status"] == "pass"


def test_run_with_baseline_regression_exits_1(fake_agent_module: str, tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    regression.save_baseline(
        baseline, [_result("simple_function", "pass")]
    )
    report = tmp_path / "report.json"
    rc = cli.main(
        [
            "run",
            "--agent",
            fake_agent_module,
            "--scenarios",
            "simple_function",
            "--baseline",
            str(baseline),
            "--output",
            str(report),
        ]
    )
    assert rc == 0  # same outcome -> no regression -> exit 0

    # A "regression" needs the fresh run to fail; simulate by a failing agent.
    baseline_ok = tmp_path / "base_ok.json"
    regression.save_baseline(
        baseline_ok, [_result("simple_function", "pass")]
    )

    def failing_factory() -> object:
        class _Noop:
            def run(self, task: str, working_dir: "str | None" = None) -> None:
                return None

        return _Noop()

    module_name = "_fake_eval_failagent"
    mod = _register_module(module_name)
    setattr(mod, "make_agent", failing_factory)
    rc = cli.main(
        [
            "run",
            "--agent",
            f"{module_name}:make_agent",
            "--scenarios",
            "simple_function",
            "--baseline",
            str(baseline_ok),
        ]
    )
    assert rc == 1
    sys.modules.pop(module_name, None)


def test_run_samples_pass_at_k(fake_agent_module: str) -> None:
    rc = cli.main(
        ["run", "--agent", fake_agent_module, "--scenarios", "simple_function", "--samples", "3"]
    )
    assert rc == 0


def test_baseline_save(
    fake_agent_module: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "baseline.json"
    rc = cli.main(
        [
            "baseline-save",
            "--agent",
            fake_agent_module,
            "--scenarios",
            "simple_function",
            "--output",
            str(out),
            "--metadata",
            "model=test",
        ]
    )
    assert rc == 0
    doc = regression.load_baseline(out)
    assert doc is not None
    assert doc["metadata"]["model"] == "test"
    assert doc["scenarios"]["simple_function"]["status"] == "pass"


def test_resolve_agent_factory_rejects_bad_spec() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli._resolve_agent_factory("no_separator")
    with pytest.raises(argparse.ArgumentTypeError):
        cli._resolve_agent_factory("no_such_module:thing")