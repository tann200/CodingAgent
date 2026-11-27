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


def test_llama_server_native_tools_thinking_off_and_read_before_write_shape(tmp_path):
    from unittest.mock import MagicMock

    from src.core.inference.adapters.llama_server_adapter import LlamaServerAdapter
    from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter
    from src.core.inference.llm_manager import get_provider_manager
    from src.core.orchestration.tool_execution_pipeline import _check_read_before_write

    adapter = LlamaServerAdapter(
        base_url="http://127.0.0.1:18080/v1",
        default_model="qwen3.5-9b",
        models=["qwen3.5-9b"],
    )
    caps = get_provider_manager().get_provider_capabilities(adapter)
    assert caps["supports_native_tools"] is True
    assert caps["provider_family"] == "local"

    payload, _ = adapter._build_payload(
        [{"role": "user", "content": "hi"}],
        "qwen3.5-9b",
        {"disable_thinking": True},
    )
    assert payload.get("chat_template_kwargs") == {"enable_thinking": False}
    assert payload.get("reasoning_budget") == 0
    assert "disable_thinking" not in payload

    base = OpenAICompatibleAdapter(
        base_url="https://api.example.com/v1",
        default_model="gpt-4o",
        models=["gpt-4o"],
    )
    base_payload, _ = base._build_payload(
        [{"role": "user", "content": "hi"}],
        "gpt-4o",
        {"disable_thinking": True},
    )
    assert "disable_thinking" not in base_payload
    assert "chat_template_kwargs" not in base_payload

    target = tmp_path / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    orch = MagicMock()
    orch.working_dir = tmp_path
    orch._session_read_files = set()
    blocked = _check_read_before_write(
        orch, "edit_file_atomic", {"path": "calc.py"}, frozenset({"edit_file_atomic"})
    )
    assert blocked is not None
    assert blocked.get("ok") is False
    assert "history" not in blocked


def test_compact_flat_tool_schemas_include_read_and_write_for_core_registry(tmp_path):
    from src.core.inference.adapters.llama_server_adapter import LlamaServerAdapter
    from src.core.orchestration.graph.runtime_policy import (
        resolve_effective_tool_schemas,
        resolve_runtime_policy,
    )
    from src.core.orchestration.orchestrator import Orchestrator

    adapter = LlamaServerAdapter(
        base_url="http://127.0.0.1:18080/v1",
        default_model="Qwen3.5-9B.Q4_K_S.gguf",
        models=["Qwen3.5-9B.Q4_K_S.gguf"],
    )
    orch = Orchestrator(
        adapter=adapter,
        working_dir=str(tmp_path),
        allow_external_working_dir=True,
        trusted=True,
        defer_provider_probe=True,
    )
    rt = resolve_runtime_policy(orch, model="Qwen3.5-9B.Q4_K_S.gguf", provider="llama_server")
    schemas = resolve_effective_tool_schemas(orch, rt, active_role="operational")
    names = {s["function"]["name"] for s in schemas}
    assert {"read_file", "write_file", "edit_file_atomic", "grep", "bash", "delete_file"}.issubset(names)


def test_message_bus_run_sync_delivery_handles_executor_shutdown():
    import asyncio
    from unittest.mock import patch

    from src.core.messaging.bus import MessageBus

    bus = MessageBus()

    async def _run():
        with patch.object(
            asyncio.get_running_loop(),
            "run_in_executor",
            side_effect=RuntimeError("cannot schedule new futures after shutdown"),
        ):
            await bus._run_sync_delivery(lambda: None)

    asyncio.run(_run())
