"""Unit tests for TASK-28: `codingagent bench-diff` and `codingagent context-report`."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from src.cli.context_report_cmd import build_context_report, run_context_report
from src.core.evaluation import cli as eval_cli
from src.core.evaluation import regression
from src.main import main as cli_main


def _write_json(path: Path, doc: Dict[str, Any]) -> Path:
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _baseline_doc(scenarios: Dict[str, Dict[str, Any]], summary: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "format_version": 1,
        "created_at": "2026-11-25T20:00:00+0000",
        "summary": summary or {},
        "metadata": {"model": "qwen3.8-9b-distill"},
        "scenarios": scenarios,
    }


class TestBenchDiff:
    def test_improved_verdict_exit_0(self, tmp_path: Path) -> None:
        base = _write_json(
            tmp_path / "base.json",
            _baseline_doc(
                {
                    "s1": {"status": "pass", "duration_seconds": 10.0, "tokens_used": 1000, "tool_calls": 4},
                    "s2": {"status": "fail", "duration_seconds": 12.0, "tokens_used": 1500, "tool_calls": 6},
                    "s3": {"status": "fail", "duration_seconds": 8.0, "tokens_used": 900, "tool_calls": 3},
                }
            ),
        )
        cand = _write_json(
            tmp_path / "cand.json",
            _baseline_doc(
                {
                    "s1": {"status": "pass", "duration_seconds": 9.0, "tokens_used": 950, "tool_calls": 3},
                    "s2": {"status": "pass", "duration_seconds": 10.0, "tokens_used": 1100, "tool_calls": 4},
                    "s3": {"status": "fail", "duration_seconds": 8.0, "tokens_used": 900, "tool_calls": 3},
                }
            ),
        )
        verdict = regression.diff_runs(
            regression.load_run_doc(base),  # type: ignore[arg-type]
            regression.load_run_doc(cand),  # type: ignore[arg-type]
            threshold=0.02,
        )
        assert verdict.verdict == "improved"
        assert verdict.exit_code == regression.EXIT_IMPROVED
        assert verdict.improvements == ["s2"]
        assert verdict.hard_regressions == []
        assert verdict.tokens_delta == -450
        assert verdict.tool_calls_delta == -3
        assert verdict.duration_delta_seconds == pytest.approx(-3.0)

    def test_hard_regression_exit_1_even_if_overall_rate_increases(self, tmp_path: Path) -> None:
        base = _baseline_doc(
            {
                "s1": {"status": "pass", "duration_seconds": 5.0},
                "s2": {"status": "fail", "duration_seconds": 5.0},
                "s3": {"status": "fail", "duration_seconds": 5.0},
            }
        )
        cand = _baseline_doc(
            {
                "s1": {"status": "fail", "duration_seconds": 5.0},
                "s2": {"status": "pass", "duration_seconds": 5.0},
                "s3": {"status": "pass", "duration_seconds": 5.0},
            }
        )
        verdict = regression.diff_runs(base, cand, threshold=0.02)
        assert verdict.verdict == "regressed"
        assert verdict.exit_code == regression.EXIT_REGRESSED
        assert verdict.hard_regressions == ["s1"]
        assert verdict.improvements == ["s2", "s3"]
        assert verdict.tokens_delta is None  # v1 compat when token fields omitted

    def test_noise_verdict_exit_2(self) -> None:
        base = _baseline_doc(
            {f"s{i}": {"status": "pass" if i < 10 else "fail"} for i in range(100)}
        )
        cand = _baseline_doc(
            {f"s{i}": {"status": "pass" if i < 11 else "fail"} for i in range(100)}
        )
        verdict = regression.diff_runs(base, cand, threshold=0.02)
        assert verdict.verdict == "noise"
        assert verdict.exit_code == regression.EXIT_NOISE
        assert verdict.pass_rate_delta == pytest.approx(0.01)

    def test_directory_resolution_and_results_list_format(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "base_dir"
        base_dir.mkdir()
        old = _write_json(base_dir / "old.json", _baseline_doc({"s1": {"status": "fail"}}))
        new = _write_json(base_dir / "new.json", _baseline_doc({"s1": {"status": "pass"}}))
        now = time.time()
        os.utime(old, (now - 100, now - 100))
        os.utime(new, (now, now))

        # Candidate uses `run --output` results-list schema
        cand = _write_json(
            tmp_path / "run_report.json",
            {
                "created_at": "2026-11-25T21:00:00",
                "summary": {"total_prompt_tokens": 200, "total_completion_tokens": 50},
                "results": [{"scenario_name": "s1", "status": "pass", "duration_seconds": 2.5}],
            },
        )
        rc = eval_cli.main(["bench-diff", str(base_dir), str(cand), "--json"])
        assert rc == regression.EXIT_NOISE

    def test_cli_error_exit_3_on_missing_or_corrupt(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        rc = cli_main(["bench-diff", str(bad), str(tmp_path / "missing.json")])
        assert rc == regression.EXIT_ERROR
        assert "not found or malformed" in capsys.readouterr().err

    def test_top_level_cli_bench_diff_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        base = _write_json(tmp_path / "b.json", _baseline_doc({"s1": {"status": "fail"}}))
        cand = _write_json(tmp_path / "c.json", _baseline_doc({"s1": {"status": "pass"}}))
        rc = cli_main(["bench-diff", str(base), str(cand), "--json"])
        assert rc == regression.EXIT_IMPROVED
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "improved"
        assert payload["improvements"] == ["s1"]


class TestContextReport:
    @pytest.mark.parametrize("tier", ["compact-flat", "standard-flat", "frontier-flat"])
    def test_build_context_report_all_tiers_offline(self, tmp_path: Path, tier: str) -> None:
        (tmp_path / "AGENTS.md").write_text("# Test Repo\nAlways run pytest.\n", encoding="utf-8")
        report = build_context_report(workdir=str(tmp_path), tier=tier)
        assert report["tier"] == tier
        sec = report["sections"]
        assert sec["base_prompt"]["tokens"] > 0
        assert sec["instruction_files"]["count"] >= 1
        assert sec["tool_schemas"]["count"] >= 10
        assert sec["tool_schemas"]["tokens"] > 0
        assert report["totals"]["upfront_tokens"] > 0
        assert report["totals"]["pct_of_32k"] > 0

    def test_cli_context_report_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli_main(["context-report", "--tier", "compact-flat", "--workdir", str(tmp_path), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tier"] == "compact-flat"
        assert "totals" in payload

    def test_invalid_tier_returns_2(self) -> None:
        class _Args:
            tier = "invalid-tier"
            workdir = None
            json = False

        assert run_context_report(_Args()) == 2
