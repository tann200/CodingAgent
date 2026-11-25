"""Unit tests for TASK-39: live 9B baseline failure remediation, eval telemetry & doctor probe."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from src.cli.doctor_cmd import test_server_native_tool_acceptance as probe_native_tools
from src.core.evaluation.scenario_evaluator import Scenario, ScenarioEvaluator
from src.core.inference.adapters.ollama_adapter import OllamaAdapter
from src.core.messaging.event_types import ModelResponse
from src.core.orchestration.graph.completion_contract import (
    evaluate_completion_contract,
    extract_requested_new_files,
)
from src.core.orchestration.pipeline.preflight import _validate_candidate_syntax
from src.tools import tools_config


class TestNamedDeliverableContract:
    def test_extract_requested_new_files_multi_file_split(self) -> None:
        task = (
            "Split monolith.py into two files: math_utils.py (containing add, multiply) "
            "and string_utils.py (containing reverse, capitalize). Update main.py."
        )
        files = extract_requested_new_files(task)
        assert "math_utils.py" in files
        assert "string_utils.py" in files

    def test_extract_requested_new_files_single_and_move(self) -> None:
        assert "hello.py" in extract_requested_new_files(
            "Create a file called hello.py with a function hello()"
        )
        assert "user_validator.py" in extract_requested_new_files(
            "Move the `UserValidator` class from validators.py into a new file user_validator.py."
        )
        assert "constants.py" in extract_requested_new_files(
            "Extract the magic number `3.14159` from geometry.py into a shared file constants.py."
        )

    def test_extract_ignores_read_only_explanation(self) -> None:
        assert extract_requested_new_files("Explain how src/main.py works.") == []

    def test_contract_rejects_when_named_deliverable_missing(self, tmp_path: Path) -> None:
        (tmp_path / "math_utils.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        verdict = evaluate_completion_contract(
            task=(
                "Split monolith.py into two files: math_utils.py (containing add) "
                "and string_utils.py (containing reverse)."
            ),
            content_text="Done splitting.",
            tool_calls_count=2,
            modified_files={"math_utils.py"},
            working_dir=str(tmp_path),
        )
        assert not verdict.is_complete
        assert verdict.error_code == "missing_named_deliverable"
        assert "string_utils.py" in (verdict.failure_reason or "")

    def test_contract_passes_once_all_deliverables_exist(self, tmp_path: Path) -> None:
        (tmp_path / "math_utils.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        (tmp_path / "string_utils.py").write_text("def reverse(s): return s[::-1]\n", encoding="utf-8")
        verdict = evaluate_completion_contract(
            task=(
                "Split monolith.py into two files: math_utils.py (containing add) "
                "and string_utils.py (containing reverse)."
            ),
            content_text="Done splitting.",
            tool_calls_count=3,
            modified_files={"math_utils.py", "string_utils.py"},
            working_dir=str(tmp_path),
        )
        assert verdict.is_complete


class TestSyntaxHintAndOllamaNormalization:
    def test_line_continuation_syntax_error_includes_newline_hint(self) -> None:
        res = _validate_candidate_syntax(None, "hello.py", "def hello():\\n    return 'hi'")
        assert res is not None
        assert res["ok"] is False
        assert "real newline characters" in res["error"]

    def test_ollama_bare_url_normalized_to_api(self) -> None:
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", models=["qwen3.8-9b-distill:latest"])
        assert adapter.base_url == "http://127.0.0.1:11434/api"

        already = OllamaAdapter(base_url="http://127.0.0.1:11434/api/", models=["qwen3.8-9b-distill:latest"])
        assert already.base_url == "http://127.0.0.1:11434/api"

    def test_ollama_generate_includes_usage_dict(self) -> None:
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434/api", models=["test-model"])
        fake_raw = {
            "model": "test-model",
            "message": {"role": "assistant", "content": "hi"},
            "prompt_eval_count": 321,
            "eval_count": 45,
        }
        with patch.object(adapter, "_chat_internal", return_value=fake_raw):
            out = adapter.generate([{"role": "user", "content": "hello"}], model="test-model")
        assert out["ok"] is True
        assert out["prompt_tokens"] == 321
        assert out["completion_tokens"] == 45
        assert out["usage"] == {
            "prompt_tokens": 321,
            "completion_tokens": 45,
            "total_tokens": 366,
        }


class TestScenarioEvaluatorAutonomousAndTelemetry:
    def test_scenario_evaluator_scopes_autonomous_and_captures_typed_tokens(self, tmp_path: Path) -> None:
        tools_config.set_autonomous(False)
        tools_config.set_autonomous_approve(set())
        observed_autonomous: Dict[str, Any] = {}

        class _FakeBus:
            def __init__(self) -> None:
                self._subs: Dict[str, list] = {}

            def subscribe(self, topic: str, fn: Any) -> None:
                self._subs.setdefault(topic, []).append(fn)

            def unsubscribe(self, topic: str, fn: Any) -> None:
                if topic in self._subs and fn in self._subs[topic]:
                    self._subs[topic].remove(fn)

            def emit(self, topic: str, payload: Any) -> None:
                for fn in list(self._subs.get(topic, [])):
                    fn(payload)

        class _FakeAgent:
            def __init__(self) -> None:
                self.working_dir = str(tmp_path)
                self.event_bus = _FakeBus()

            def run(self, task: str, working_dir: str) -> None:
                observed_autonomous["enabled"] = tools_config.is_autonomous()
                observed_autonomous["run_tests"] = tools_config.autonomous_approval_allowed("run_tests")
                self.event_bus.emit(
                    "model.response",
                    ModelResponse(
                        provider="ollama",
                        model="qwen3.8-9b-distill:latest",
                        prompt_tokens=500,
                        completion_tokens=80,
                        total_tokens=580,
                        latency=0.5,
                        ts=1.0,
                    ),
                )
                (Path(working_dir) / "out.py").write_text("x = 1\n", encoding="utf-8")

        ev = ScenarioEvaluator(workdir=str(tmp_path))
        sc = Scenario(
            name="s_eval_auto",
            description="test",
            task="Create out.py",
            expected_files={"out.py": "x = 1"},
        )
        res = ev.run_scenario(sc, _FakeAgent)
        ev.cleanup()

        assert res.status == "pass"
        assert observed_autonomous == {"enabled": True, "run_tests": True}
        assert tools_config.is_autonomous() is False  # cleanly restored!
        assert res.prompt_tokens == 500
        assert res.completion_tokens == 80


class TestDoctorProbe:
    def test_doctor_ollama_probe_sends_num_predict_and_default_30s_timeout(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("src.cli.doctor_cmd.requests.post", return_value=mock_resp) as m_post:
            ok, err = probe_native_tools(
                "http://localhost:11434",
                "ollama",
                "tobestyledintro/qwen3.8-9b-distill:latest",
                [],
            )
        assert ok is True
        assert err is None
        _, kwargs = m_post.call_args
        assert kwargs["timeout"] == 30.0
        assert kwargs["json"]["options"]["num_predict"] == 8
