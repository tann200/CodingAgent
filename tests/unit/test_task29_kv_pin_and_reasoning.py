"""Unit tests for TASK-29: KV slot pinning, patient timeout, /reasoning & reasoning_content rescue."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from src.core.inference.adapters.llama_server_adapter import LlamaServerAdapter
from src.core.inference.adapters.ollama_adapter import OllamaAdapter
from src.core.inference.thinking_utils import (
    ReasoningEffort,
    apply_reasoning_to_payload,
    get_active_reasoning_effort,
    resolve_reasoning_effort,
    set_active_reasoning_effort,
)
from src.core.orchestration.tool_parser import parse_response_tool_call


@pytest.fixture(autouse=True)
def _reset_reasoning_state():
    set_active_reasoning_effort(None)
    yield
    set_active_reasoning_effort(None)


class TestReasoningPolicy:
    def test_compact_reasoning_model_defaults_to_low_and_bumps_on_retry(self) -> None:
        eff0 = resolve_reasoning_effort(
            model_id="tobestyledintro/qwen3.8-9b-distill:latest",
            tier="compact",
            verification_retries=0,
        )
        assert eff0 == ReasoningEffort.LOW

        eff1 = resolve_reasoning_effort(
            model_id="tobestyledintro/qwen3.8-9b-distill:latest",
            tier="compact",
            verification_retries=1,
        )
        assert eff1 == ReasoningEffort.MEDIUM

    def test_explicit_override_and_reset(self) -> None:
        set_active_reasoning_effort("high")
        assert get_active_reasoning_effort() == ReasoningEffort.HIGH
        assert (
            resolve_reasoning_effort(model_id="qwen3.8-9b-distill", tier="compact")
            == ReasoningEffort.HIGH
        )
        set_active_reasoning_effort("reset")
        assert get_active_reasoning_effort() is None

    def test_apply_reasoning_across_providers(self) -> None:
        # llama_server LOW
        p_llama: Dict[str, Any] = {"messages": []}
        apply_reasoning_to_payload("llama_server", p_llama, ReasoningEffort.LOW, model_id="qwen3.8-9b")
        assert p_llama["chat_template_kwargs"]["enable_thinking"] is True
        assert p_llama["reasoning_budget"] == 1024
        assert p_llama["max_tokens"] == 2048

        # ollama LOW
        p_ollama: Dict[str, Any] = {"messages": []}
        apply_reasoning_to_payload("ollama", p_ollama, ReasoningEffort.LOW, model_id="qwen3.8-9b")
        assert p_ollama["think"] is True
        assert p_ollama["options"]["num_predict"] == 2048

        # ollama OFF
        p_ollama_off: Dict[str, Any] = {"messages": []}
        apply_reasoning_to_payload("ollama", p_ollama_off, ReasoningEffort.OFF, model_id="qwen3.8-9b")
        assert p_ollama_off["think"] is False

        # anthropic MEDIUM
        p_anth: Dict[str, Any] = {"messages": []}
        apply_reasoning_to_payload("anthropic", p_anth, ReasoningEffort.MEDIUM, model_id="claude-sonnet-4")
        assert p_anth["thinking"] == {"type": "enabled", "budget_tokens": 2560}

        # openai non-reasoning model omits reasoning_effort safely
        p_oai_plain: Dict[str, Any] = {"messages": []}
        apply_reasoning_to_payload("openai", p_oai_plain, ReasoningEffort.HIGH, model_id="gpt-4o")
        assert "reasoning_effort" not in p_oai_plain

        # openai reasoning model sets reasoning_effort
        p_oai_r: Dict[str, Any] = {"messages": []}
        apply_reasoning_to_payload("openai", p_oai_r, ReasoningEffort.MEDIUM, model_id="o3-mini")
        assert p_oai_r["reasoning_effort"] == "medium"


class TestLlamaServerAndOllamaAdapters:
    def test_llama_server_pins_slot_0_and_omits_for_parallel_worker(self) -> None:
        adapter = LlamaServerAdapter(base_url="http://localhost:8080/v1", default_model="qwen3.8-9b")
        assert adapter.DEFAULT_TIMEOUT >= 600.0

        p_single, _ = adapter._build_payload(
            [{"role": "user", "content": "hi"}],
            "qwen3.8-9b",
            {"policy_tier": "compact"},
        )
        assert p_single["cache_prompt"] is True
        assert p_single["id_slot"] == 0
        assert p_single["reasoning_budget"] == 1024

        p_worker, _ = adapter._build_payload(
            [{"role": "user", "content": "hi"}],
            "qwen3.8-9b",
            {"parallel_worker": True, "policy_tier": "compact"},
        )
        assert p_worker["cache_prompt"] is True
        assert "id_slot" not in p_worker

    def test_ollama_applies_reasoning_budget_clamp(self) -> None:
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434/api", models=["qwen3.8-9b-distill:latest"])
        assert adapter.DEFAULT_TIMEOUT >= 600.0
        captured: Dict[str, Any] = {}

        def _fake_post(url: str, payload: Dict[str, Any], timeout: Any = None) -> Any:
            captured["payload"] = payload
            resp = MagicMock()
            resp.json.return_value = {"message": {"role": "assistant", "content": "ok"}}
            return resp

        with patch.object(adapter, "_post_generation_with_retry", side_effect=_fake_post):
            adapter._chat_internal(
                [{"role": "user", "content": "hi"}],
                model="qwen3.8-9b-distill:latest",
                policy_tier="compact",
                verification_retries=0,
            )
        assert captured["payload"]["think"] is True
        assert captured["payload"]["options"]["num_predict"] == 2048


class TestReasoningContentRescue:
    def test_rescues_tool_call_when_content_empty(self) -> None:
        reasoning = 'Let me read the file.\n```json\n{"name": "read_file", "arguments": {"path": "app.py"}}\n```'
        parsed = parse_response_tool_call("", reasoning)
        assert parsed == {"name": "read_file", "arguments": {"path": "app.py"}}

    def test_prefers_content_when_present(self) -> None:
        content = '{"name": "list_files", "arguments": {"path": "."}}'
        reasoning = '{"name": "read_file", "arguments": {"path": "other.py"}}'
        parsed = parse_response_tool_call(content, reasoning)
        assert parsed == {"name": "list_files", "arguments": {"path": "."}}

    def test_does_not_rescue_when_content_has_non_tool_text(self) -> None:
        parsed = parse_response_tool_call(
            "Here is the explanation.",
            '{"name": "read_file", "arguments": {"path": "a.py"}}',
        )
        assert parsed is None
