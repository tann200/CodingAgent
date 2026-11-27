"""Regression tests for streaming token telemetry, regression tool_calls counting,
macOS sandbox-exec python/execvp handling, and mutating-task text-only nudge recovery."""

from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from src.core.evaluation.regression import _scenario_outcomes
from src.core.evaluation.scenario_evaluator import ScenarioResult
from src.core.inference.streaming import consume_generator_stream
from src.core.orchestration.loop_guards import build_incomplete_turn_nudge
from src.tools import sandbox as _sandbox
from src.tools._bash_exec import bash


def test_consume_ollama_stream_populates_usage_and_tokens() -> None:
    chunks = [
        {"message": {"role": "assistant", "content": "Hello "}, "done": False},
        {
            "message": {"role": "assistant", "content": "world"},
            "done": True,
            "prompt_eval_count": 412,
            "eval_count": 37,
        },
    ]
    res = consume_generator_stream(chunks, provider="ollama", model="qwen3.8-9b-distill:latest")
    assert res["ok"] is True
    assert res["streamed"] is True
    assert res["prompt_tokens"] == 412
    assert res["completion_tokens"] == 37
    assert res["total_tokens"] == 449
    assert res["usage"] == {
        "prompt_tokens": 412,
        "completion_tokens": 37,
        "total_tokens": 449,
    }


def test_regression_scenario_outcomes_uses_tool_calls_total_when_list_empty() -> None:
    now = datetime.datetime.now()
    sr = ScenarioResult(
        scenario_name="simple_function",
        status="pass",
        start_time=now,
        end_time=now,
        duration_seconds=12.5,
        prompt_tokens=800,
        completion_tokens=120,
        tool_calls=[],
        tool_calls_total=4,
        malformed_tool_calls=0,
    )
    outcomes = _scenario_outcomes([sr])
    rec = outcomes["simple_function"]
    assert rec["prompt_tokens"] == 800
    assert rec["completion_tokens"] == 120
    assert rec["tokens_used"] == 920
    assert rec["tool_calls"] == 4


def test_sandbox_exec_execvp_enoent_returns_127_not_profile_error(tmp_path: Path) -> None:
    fake_proc = subprocess.CompletedProcess(
        args=["sandbox-exec", "-f", "/tmp/p.sb", "nonexistent_bin"],
        returncode=71,
        stdout="",
        stderr="sandbox-exec: execvp() of 'nonexistent_bin' failed: No such file or directory\n",
    )
    with (
        patch.object(_sandbox, "_BWRAP_AVAILABLE", False),
        patch.object(_sandbox, "_SANDBOX_EXEC_PATH", Path("/usr/bin/sandbox-exec")),
        patch.object(_sandbox, "sandbox_exec_enforced", return_value=True),
        patch.object(_sandbox, "_run_subprocess_proc_group", return_value=fake_proc) as mock_run,
    ):
        res = _sandbox.run_sandboxed(
            ["nonexistent_bin"], cwd=tmp_path, timeout=5, network=False, capture_output=True, text=True
        )
        assert res.returncode == 127
        # Active Python bin directory is prepended to sandbox PATH so python/pytest resolve cleanly
        run_env = mock_run.call_args.kwargs["env"]
        assert str(Path(sys.executable).resolve().parent) in run_env["PATH"].split(":")


def test_bash_resolves_bare_python_when_missing_from_path(tmp_path: Path) -> None:
    fake_ok = subprocess.CompletedProcess(args=[sys.executable, "-c", "print(1)"], returncode=0, stdout="1\n", stderr="")
    with (
        patch("src.tools._bash_exec._is_os_sandbox_enforced", return_value=True),
        patch("src.tools._bash_exec.shutil.which", return_value=None),
        patch("src.tools.sandbox.run_sandboxed", return_value=fake_ok) as mock_run,
    ):
        out = bash('python -c "print(1)"', workdir=tmp_path)
        assert out["status"] == "ok"
        assert mock_run.call_args.args[0][0] == sys.executable


def test_build_incomplete_turn_nudge_gives_explicit_file_tool_guidance(tmp_path: Path) -> None:
    nudge = build_incomplete_turn_nudge(
        "unverified_text_only_completion",
        "Task requested code or file modifications, but no mutating tools were executed.",
    )
    assert "write_file" in nudge
    assert "edit_file_atomic" in nudge
    assert "Do NOT reply with prose" in nudge

    # Ensure ScenarioEvaluator handles UsageTurnSummary input_tokens/output_tokens
    from src.core.evaluation.scenario_evaluator import Scenario, ScenarioEvaluator

    ev = ScenarioEvaluator()
    bus_handlers: dict = {}

    class _FakeBus:
        def subscribe(self, topic, fn):
            bus_handlers.setdefault(topic, []).append(fn)

        def unsubscribe(self, topic, fn):
            if topic in bus_handlers and fn in bus_handlers[topic]:
                bus_handlers[topic].remove(fn)

    class _FakeAgent:
        def __init__(self):
            self.event_bus = _FakeBus()
            self.working_dir = tmp_path

        def run_agent_once(self, *args, **kwargs):
            for fn in bus_handlers.get("tool.execute.start", []):
                fn({"tool": "write_file"})
            for fn in bus_handlers.get("usage.turn_summary", []):
                fn({"input_tokens": 555, "output_tokens": 66})
            (Path(self.working_dir) / "out.txt").write_text("ok", encoding="utf-8")
            return {"ok": True, "assistant_message": "done"}

    sc = Scenario(
        name="telemetry_check",
        description="check telemetry",
        task="create out.txt",
        expected_files={"out.txt": "ok"},
    )
    res = ev.run_scenario(sc, lambda: _FakeAgent())
    assert res.status == "pass"
    assert res.prompt_tokens == 555
    assert res.completion_tokens == 66
    assert res.tool_calls_total == 1
    ev.cleanup()
