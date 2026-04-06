"""tests/unit/test_orchestrator_system_prompt_auto.py

S10-D: Refactored to use MockAdapter — no live LLM required.

Verifies that the Orchestrator:
  - Inserts a system prompt as the first message before calling the model
  - Replaces a stale system prompt on a subsequent run
"""

from __future__ import annotations

import pytest

from src.core.inference.adapters.mock_adapter import MockAdapter
from src.core.orchestration.orchestrator import Orchestrator

# Every graph node that calls call_model at module-load time needs patching.
_CALL_MODEL_TARGETS = [
    "src.core.orchestration.graph.nodes.perception_node.call_model",
    "src.core.orchestration.graph.nodes.planning_node.call_model",
    "src.core.orchestration.graph.nodes.execution_node.call_model",
    "src.core.orchestration.graph.nodes.debug_node.call_model",
    "src.core.orchestration.graph.nodes.replan_node.call_model",
    "src.core.inference.llm_manager.call_model",
    "src.core.inference.llm_manager._call_model_internal",
]


def _fake_call_model_response():
    """Return a minimal model response that terminates the agent loop."""
    return {
        "ok": True,
        "provider": "mock",
        "model": "mock-model",
        "prompt_tokens": 5,
        "completion_tokens": 5,
        "total_tokens": 10,
        "content": "OK",
        "choices": [
            {"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
        ],
    }


def _build_orch(tmp_path, monkeypatch) -> Orchestrator:
    adapter = MockAdapter(responses=["OK"])

    async def mock_call_model(messages, model=None, provider=None, *args, **kwargs):
        return _fake_call_model_response()

    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, mock_call_model)
        except AttributeError:
            pass

    monkeypatch.setattr(
        "src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator.Orchestrator._background_model_check",
        lambda self: None,
    )
    return Orchestrator(
        adapter=adapter,
        working_dir=str(tmp_path),
        allow_external_working_dir=True,
        message_max_tokens=8000,
    )


def test_orchestrator_inserts_system_prompt(tmp_path, monkeypatch):
    orch = _build_orch(tmp_path, monkeypatch)
    orch.msg_mgr.clear()
    _ = orch.run_agent_once(None, [{"role": "user", "content": "hello"}], {})
    msgs = orch.msg_mgr.all()
    assert msgs, "MessageManager should contain messages after run_agent_once"
    assert msgs[0].get("role") == "system", "First message should be the system prompt"
    content = msgs[0].get("content", "")
    assert "operational" in content.lower()


def test_system_prompt_replaced_when_different(tmp_path, monkeypatch):
    orch = _build_orch(tmp_path, monkeypatch)
    orch.msg_mgr.clear()
    orch.msg_mgr.append("system", "OLD PROMPT")
    orch.run_agent_once(None, [{"role": "user", "content": "hello again"}], {})
    msgs = orch.msg_mgr.all()
    assert msgs[0].get("role") == "system"
    assert msgs[0].get("content") != "OLD PROMPT"
    content = msgs[0].get("content", "")
    assert "operational" in content.lower()
