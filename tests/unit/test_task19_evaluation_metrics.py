"""Unit tests for TASK-19 evaluation gate metrics collection & reporting."""

from __future__ import annotations

from pathlib import Path

from src.core.evaluation.scenario_evaluator import Scenario, ScenarioEvaluator
from src.core.orchestration.event_bus import EventBus


def test_scenario_evaluator_collects_pass_at_1_edit_malformed_verify_and_latency_metrics(
    tmp_path: Path,
):
    ev = ScenarioEvaluator(workdir=str(tmp_path))
    s_pass = Scenario(
        name="good_edit",
        description="valid edit",
        task="Write add.py",
        expected_files={"add.py": "def add"},
        test_command="python -c 'from add import add; assert add(1, 2) == 3'",
    )
    s_fail = Scenario(
        name="bad_verify",
        description="failing verification",
        task="Write broken.py",
        expected_files={"broken.py": "def mul"},
        test_command="python -c 'from broken import mul; assert mul(2, 3) == 6'",
    )
    ev.add_scenario(s_pass)
    ev.add_scenario(s_fail)

    def make_good_agent():
        class Agent:
            def __init__(self):
                self.event_bus = EventBus()

            def run(self, task, working_dir=None):
                wd = Path(working_dir)
                self.event_bus.publish("model.response", {"prompt_tokens": 120, "completion_tokens": 30, "latency": 0.4})
                self.event_bus.publish("tool.execute.start", {"tool": "write_file"})
                if "add.py" in task:
                    (wd / "add.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
                else:
                    self.event_bus.publish("tool.execute.start", {"tool": "write_file"})
                    self.event_bus.publish("tool.execute.error", {"tool": "write_file", "error": "malformed json args"})
                    (wd / "broken.py").write_text("def mul(a, b): return a + b\n", encoding="utf-8")

        return Agent()

    results = ev.run_evaluation(make_good_agent)
    assert len(results) == 2
    assert results[0].status == "pass"
    assert results[0].edit_accurate is True
    assert results[0].verification_passed is True
    assert results[0].prompt_tokens == 120
    assert results[0].prefill_latency_ms == 400.0

    assert results[1].status == "fail"
    assert results[1].edit_accurate is True
    assert results[1].verification_attempted is True
    assert results[1].verification_passed is False
    assert results[1].malformed_tool_calls == 1

    summary = ev.get_summary(results)
    assert summary["pass_at_1"] == 0.5
    assert summary["edit_accuracy"] == 1.0
    assert summary["verification_pass_rate"] == 0.5
    assert summary["malformed_tool_call_rate"] == 1 / 3
    assert summary["total_prompt_tokens"] == 240
    assert summary["total_completion_tokens"] == 60
    assert summary["avg_prefill_latency_ms"] == 400.0


def test_active_runtime_context_routes_unspecified_provider_calls():
    import asyncio
    from unittest.mock import MagicMock

    from src.core.inference.llm_manager import active_runtime_context, call_model

    fake_adapter = MagicMock()
    fake_adapter.default_model = "local-gguf"
    fake_adapter.models = ["local-gguf"]
    fake_adapter.chat.return_value = {
        "ok": True,
        "provider": "llama_server",
        "model": "local-gguf",
        "choices": [{"message": {"role": "assistant", "content": "Title"}}],
    }

    async def _run():
        with active_runtime_context(
            provider="llama_server",
            model="local-gguf",
            adapter=fake_adapter,
        ):
            return await call_model([{"role": "user", "content": "hi"}])

    res = asyncio.run(_run())
    assert res.get("ok") is True
    assert fake_adapter.chat.called
    _, kwargs = fake_adapter.chat.call_args
    assert kwargs.get("model") == "local-gguf"
