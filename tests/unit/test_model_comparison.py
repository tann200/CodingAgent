"""Audit PHASE-4 item 4.5: model comparison evaluation."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from src.core.evaluation import cli
from src.core.evaluation import compare
from src.core.evaluation.scenario_evaluator import (
    ScenarioResult,
    get_default_scenarios,
)


def _result(name: str, status: str) -> ScenarioResult:
    now = datetime.now()
    return ScenarioResult(
        scenario_name=name,
        status=status,
        start_time=now,
        end_time=now,
        duration_seconds=1.0,
    )


class _Passing:
    def run(self, task: str, working_dir: "str | None" = None) -> None:
        assert working_dir is not None
        (Path(working_dir) / "hello.py").write_text(
            "def hello():\n    return 'Hello World'\n"
        )


class _Noop:
    def run(self, task: str, working_dir: "str | None" = None) -> None:
        return None


def _register(name: str, factory) -> str:
    mod = types.ModuleType(name)
    setattr(mod, "make_agent", factory)
    sys.modules[name] = mod
    return f"{name}:make_agent"


class TestCompareModels:
    def test_ranking_and_winners(self) -> None:
        runs = {
            "alpha": {
                "results": [_result("a", "pass"), _result("b", "fail")],
                "summary": {"pass_rate": 0.5},
            },
            "beta": {
                "results": [_result("a", "pass"), _result("b", "pass")],
                "summary": {"pass_rate": 1.0},
            },
        }
        cmp = compare.compare_models(runs)
        assert cmp["ranking"] == ["beta", "alpha"]
        assert cmp["scenarios"]["a"]["winners"] == ["alpha", "beta"]
        assert cmp["scenarios"]["b"]["winners"] == ["beta"]
        assert cmp["pass_rates"] == {"alpha": 0.5, "beta": 1.0}
        assert cmp["tied_scenarios"] == 1

    def test_ties_and_missing_scenarios(self) -> None:
        runs: dict[str, dict] = {
            "a": {"results": [_result("x", "pass")], "summary": {"pass_rate": 1.0}},
            "b": {"results": [], "summary": {"pass_rate": 0.0}},
        }
        cmp = compare.compare_models(runs)
        assert cmp["scenarios"]["x"]["statuses"] == {"a": "pass"}
        assert cmp["scenarios"]["x"]["winners"] == ["a"]
        assert cmp["tied_scenarios"] == 0

    def test_dict_results_compatible(self) -> None:
        runs = {
            "m": {
                "results": [{"scenario_name": "swe-1", "status": "pass"}],
                "summary": {"pass_rate": 1.0},
            }
        }
        cmp = compare.compare_models(runs)
        assert cmp["scenarios"]["swe-1"]["winners"] == ["m"]


class TestSaveComparison:
    def test_exports_json(self, tmp_path: Path) -> None:
        runs = {
            "alpha": {
                "results": [_result("a", "pass")],
                "summary": {"pass_rate": 1.0},
            }
        }
        cmp = compare.compare_models(runs)
        out = tmp_path / "cmp.json"
        compare.save_comparison(out, runs, cmp, metadata={"suite": "test"})
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["kind"] == "model-comparison"
        assert doc["metadata"]["suite"] == "test"
        assert doc["comparison"]["ranking"] == ["alpha"]
        assert doc["models"]["alpha"]["results"][0]["status"] == "pass"


class TestRunModels:
    def test_run_models_end_to_end(self, tmp_path: Path) -> None:
        scenarios = [s for s in get_default_scenarios() if s.name == "simple_function"]
        runs = compare.run_models(
            {"good": lambda: _Passing(), "bad": lambda: _Noop()},
            scenarios,
            workdir=str(tmp_path / "runs"),
        )
        assert runs["good"]["summary"]["passed"] == 1
        assert runs["bad"]["summary"]["passed"] == 0
        assert runs["good"]["results"][0].status == "pass"

    def test_run_models_samples(self, tmp_path: Path) -> None:
        scenarios = [s for s in get_default_scenarios() if s.name == "simple_function"]
        runs = compare.run_models(
            {"good": lambda: _Passing()},
            scenarios,
            samples=2,
            workdir=str(tmp_path / "runs"),
        )
        assert runs["good"]["summary"]["passed"] == 1
        assert len(runs["good"]["passatk"]) == 1


class TestCliCompare:
    def test_compare_subcommand(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        good = _register("_cmp_good", lambda: _Passing())
        bad = _register("_cmp_bad", lambda: _Noop())
        rc = cli.main(
            [
                "compare",
                "--model",
                f"good={good}",
                "--model",
                f"bad={bad}",
                "--scenarios",
                "simple_function",
                "--workdir",
                str(tmp_path / "runs"),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "good" in out and "bad" in out
        assert "winner=good" in out
        assert "pass_rate=100.0%" in out

    def test_compare_writes_report(self, tmp_path: Path) -> None:
        good = _register("_cmp_good2", lambda: _Passing())
        out = tmp_path / "report.json"
        rc = cli.main(
            [
                "compare",
                "--model",
                f"good={good}",
                "--scenarios",
                "simple_function",
                "--workdir",
                str(tmp_path / "runs"),
                "--output",
                str(out),
            ]
        )
        assert rc == 0
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["comparison"]["ranking"] == ["good"]
        assert doc["models"]["good"]["summary"]["passed"] == 1

    def test_compare_requires_model(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            cli.main(["compare", "--scenarios", "simple_function"])


def test_package_exports_compare() -> None:
    import src.core.evaluation as ev

    for name in ("compare_models", "run_models", "save_comparison"):
        assert hasattr(ev, name), name
        assert name in ev.__all__