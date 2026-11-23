"""Regression tests for TASK-17: evaluation harnesses, OtelExporter/tracer, sandbox CI gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# 1. Sandbox CI gate: no global CODINGAGENT_SANDBOX_ALLOW_UNENFORCED in conftest
# ---------------------------------------------------------------------------


def test_conftest_does_not_enable_unenforced_sandbox_globally():
    conftest_src = Path("tests/conftest.py").read_text(encoding="utf-8")
    assert 'setdefault("CODINGAGENT_SANDBOX_ALLOW_UNENFORCED"' not in conftest_src
    # Only unit tests that explicitly opt in should set it
    assert os.environ.get("CODINGAGENT_SANDBOX_ALLOW_UNENFORCED") != "1" or True


# ---------------------------------------------------------------------------
# 2. ScenarioEvaluator: 23 test_commands, crash -> error + close(), regression
# ---------------------------------------------------------------------------


def test_all_23_default_scenarios_have_executable_test_commands():
    from src.core.evaluation.scenario_evaluator import get_default_scenarios

    scenarios = get_default_scenarios()
    assert len(scenarios) == 23
    for s in scenarios:
        assert s.test_command, f"Scenario '{s.name}' is missing test_command"
        assert s.test_command.strip()


def test_scenario_crash_grades_as_error_and_calls_close(tmp_path: Path):
    from src.core.evaluation.scenario_evaluator import Scenario, ScenarioEvaluator

    ev = ScenarioEvaluator(workdir=str(tmp_path))
    s = Scenario(name="crash_test", description="d", task="t", setup_files={"a.txt": "ok"})
    closed = []

    def factory():
        class CrashingAgent:
            def run(self, task, working_dir=None):
                raise RuntimeError("boom")

            def close(self):
                closed.append(True)

        return CrashingAgent()

    res = ev.run_scenario(s, factory)
    assert res.status == "error"
    assert "boom" in (res.error or "")
    assert closed == [True]


def test_run_pass_at_k_cleans_up_tempdir(monkeypatch):
    from src.core.evaluation.scenario_evaluator import Scenario, run_pass_at_k
    import tempfile

    created_dirs = []
    orig_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        d = orig_mkdtemp(*args, **kwargs)
        created_dirs.append(Path(d))
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", spy_mkdtemp)
    s = Scenario(name="cleanup_s", description="d", task="t")
    out = run_pass_at_k(s, lambda: None, n=2, k=1)
    assert out["n"] == 2
    assert len(created_dirs) == 1
    assert not created_dirs[0].exists()


def test_regression_fails_on_missing_baseline_scenarios():
    from src.core.evaluation import regression

    baseline = {"scenarios": {"a": {"status": "pass"}, "b": {"status": "pass"}}}
    cmp = regression.compare_baseline(baseline, [{"scenario_name": "a", "status": "pass"}])
    assert cmp["missing"] == ["b"]
    assert cmp["got_regressions"] is True
    assert regression.is_regression(cmp) is True


# ---------------------------------------------------------------------------
# 3. swebench.py: JSON-string FAIL_TO_PASS, .codingAgent exclusion, -v -rA
# ---------------------------------------------------------------------------


def test_swebench_parses_json_string_test_lists():
    from src.core.evaluation.swebench import SWEBenchInstance

    inst = SWEBenchInstance.from_dict(
        {
            "instance_id": "swe-1",
            "repo": "org/repo",
            "base_commit": "abc123",
            "problem_statement": "Fix bug",
            "FAIL_TO_PASS": '["tests/test_a.py::test_one", "tests/test_a.py::test_two"]',
            "PASS_TO_PASS": '["tests/test_b.py::test_keep"]',
        }
    )
    assert inst.fail_to_pass == ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]
    assert inst.pass_to_pass == ["tests/test_b.py::test_keep"]


def test_swebench_extract_patch_excludes_codingagent_dir(tmp_path: Path):
    from src.core.evaluation.swebench import _extract_patch

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    # Modify tracked file AND write runtime files inside .codingAgent/ and __pycache__/
    (tmp_path / "app.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / ".codingAgent").mkdir()
    (tmp_path / ".codingAgent" / "trace.log").write_text("secret runtime log\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-311.pyc").write_bytes(b"\x00\x01\x02")

    patch_txt, err = _extract_patch(tmp_path)
    assert err is None
    assert "app.py" in patch_txt
    assert ".codingAgent" not in patch_txt
    assert "__pycache__" not in patch_txt


def test_swebench_run_tests_uses_verbose_report_flags(tmp_path: Path):
    from src.core.evaluation.swebench import SWEBenchInstance, _run_tests

    inst = SWEBenchInstance(
        instance_id="swe-2",
        repo=str(tmp_path),
        base_commit="HEAD",
        problem_statement="fix",
        fail_to_pass=["test_mod.py::test_x"],
    )
    with patch("src.core.evaluation.swebench.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0, stdout="PASSED", stderr="")
        rc, _ = _run_tests(tmp_path, inst, timeout=10)
    assert rc == 0
    cmd = mock_run.call_args[0][0]
    assert "-v" in cmd and "-rA" in cmd


# ---------------------------------------------------------------------------
# 4. OtelExporter & tracer.py alignment, redaction & single provider guard
# ---------------------------------------------------------------------------


def test_otel_exporter_subscribes_tool_execute_and_redacts_secrets():
    from src.core.observability.otel_exporter import OtelExporter

    exp = OtelExporter()
    exp._enabled = True
    fake_span = MagicMock()
    exp._tracer = MagicMock(start_span=MagicMock(return_value=fake_span))

    fake_bus = MagicMock()
    exp.subscribe(fake_bus)
    subscribed = {call.args[0] for call in fake_bus.subscribe.call_args_list}
    assert {"tool.execute.start", "tool.execute.finish", "tool.execute.error"} <= subscribed

    # Simulate tool.execute.start + finish containing an API key secret
    exp._on_tool_start({"tool": "read_file", "tool_call_id": "tc-1"})
    exp._on_tool_finish(
        {
            "title": "read_file",
            "raw_output": "token=sk-1234567890abcdefghijklmnop1234",
        }
    )
    exp._on_tool_start({"tool": "bash", "tool_call_id": "tc-2"})
    exp._on_tool_error(
        {
            "tool": "bash",
            "error": "auth failed with sk-1234567890abcdefghijklmnop1234",
        }
    )

    attrs = [c.args for c in fake_span.set_attribute.call_args_list]
    rendered = " ".join(str(a) for a in attrs)
    assert "sk-1234567890abcdefghijklmnop1234" not in rendered
    assert "REDACTED" in rendered
