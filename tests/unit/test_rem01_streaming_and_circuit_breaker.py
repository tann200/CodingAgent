"""Regression tests for REM-01: streaming payloads, Ollama errors, SSE UTF-8 & CircuitBreaker cancel."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.core.inference.adapters.anthropic_adapter import AnthropicAdapter
from src.core.inference.adapters.ollama_adapter import OllamaAdapter
from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter
from src.core.inference.circuit_breaker import CircuitBreaker
from src.core.inference.streaming import decode_sse_line, extract_stream_deltas


class TestStreamingPayloads:
    def test_openai_compat_sets_stream_true_in_json_payload(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:8000/v1",
            api_key="test-key",
            models=["gpt-4o"],
        )
        captured: dict = {}
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.raise_for_status.return_value = None

        def fake_retry(ep, payload, stream, timeout=None):
            captured["ep"] = ep
            captured["payload"] = dict(payload)
            captured["stream"] = stream
            return fake_resp

        with patch.object(adapter, "_execute_with_retry", side_effect=fake_retry):
            res = adapter._chat_internal(
                [{"role": "user", "content": "hi"}],
                model="gpt-4o",
                stream=True,
            )

        assert res is fake_resp
        assert captured["stream"] is True
        assert captured["payload"]["stream"] is True

    def test_openai_compat_handles_none_usage_fields(self) -> None:
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:8000/v1",
            api_key="test-key",
            models=["gpt-4o"],
        )
        raw = {
            "model": "gpt-4o",
            "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": None, "completion_tokens": None},
        }
        with patch.object(adapter, "_chat_internal", return_value=raw):
            res = adapter.generate([{"role": "user", "content": "hi"}], model="gpt-4o", stream=False)
        assert res["ok"] is True
        assert res["prompt_tokens"] == 0
        assert res["completion_tokens"] == 0
        assert res["total_tokens"] == 0

    def test_anthropic_sets_stream_true_and_synthesizes_tool_ids_and_honors_base_url(self) -> None:
        adapter = AnthropicAdapter(
            base_url="https://proxy.example.internal/v1",
            api_key="sk-ant-test",
            models=["claude-sonnet-4-6"],
        )
        captured: dict = {}
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.raise_for_status.return_value = None

        def fake_retry(ep, payload, stream, timeout=None):
            captured["ep"] = ep
            captured["payload"] = dict(payload)
            captured["stream"] = stream
            return fake_resp

        messages = [
            {"role": "user", "content": "run ls"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
            },
            {"role": "tool", "content": "ok"},
        ]
        with patch.object(adapter, "_execute_with_retry", side_effect=fake_retry):
            adapter._chat_internal(messages, model="claude-sonnet-4-6", stream=True)

        assert captured["ep"] == "https://proxy.example.internal/v1/messages"
        assert captured["payload"]["stream"] is True
        # Synthesized non-empty tool_use id matches paired tool_result
        asst_blocks = captured["payload"]["messages"][1]["content"]
        tool_use = next(b for b in asst_blocks if b["type"] == "tool_use")
        tool_res = captured["payload"]["messages"][2]["content"][0]
        assert tool_use["id"] != ""
        assert tool_res["tool_use_id"] == tool_use["id"]

        # Models endpoint derives from custom self.base_url
        assert adapter._models_endpoints() == ["https://proxy.example.internal/v1/models"]


class TestOllamaStreamErrorAndSSE:
    def test_ollama_stream_error_dict_returns_ok_false(self) -> None:
        adapter = OllamaAdapter(base_url="http://localhost:11434", models=["qwen3:8b"])
        with patch.object(
            adapter,
            "_chat_internal",
            return_value={"meta": {"error": "http_error", "status_code": 500}},
        ):
            res = adapter.generate(
                [{"role": "user", "content": "hi"}],
                model="qwen3:8b",
                stream=True,
            )
        assert res["ok"] is False
        assert res["error"] == "http_error"

    def test_decode_sse_line_does_not_wedge_on_invalid_utf8(self) -> None:
        carry = bytearray()
        # Malformed UTF-8 byte in line 1 must not wedge line 2
        bad = b"data: hello \xff world"
        out1 = decode_sse_line(bad, carry=carry)
        assert out1 is not None and "hello" in out1 and "world" in out1
        assert len(carry) == 0

        good = b'data: {"choices": [{"delta": {"content": "next"}}]}'
        out2 = decode_sse_line(good, carry=carry)
        assert out2 == '{"choices": [{"delta": {"content": "next"}}]}'

    def test_extract_stream_deltas_routes_is_reasoning_content(self) -> None:
        chunk = {"choices": [{"delta": {"content": "thinking...", "is_reasoning": True}}]}
        res = extract_stream_deltas(chunk)
        assert res is not None
        _delta, reasoning, content = res
        assert reasoning == "thinking..."
        assert content == ""


class TestCircuitBreakerCancellation:
    def test_release_probe_unblocks_half_open_after_cancellation(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        assert cb._state == CircuitBreaker.OPEN
        cb._opened_at -= 1.0  # simulate recovery_timeout elapsed

        assert cb.allow_request() is True
        assert cb.allow_request() is False  # second concurrent probe blocked
        cb.release_probe()
        assert cb.allow_request() is True  # unblocked cleanly after cancellation

    def test_call_model_releases_probe_on_cancelled_error(self, monkeypatch) -> None:
        from src.core.inference import llm_manager

        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        cb._opened_at -= 1.0

        async def _cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(llm_manager, "get_circuit_breaker", lambda _k: cb)
        monkeypatch.setattr(llm_manager, "_call_model_internal", _cancelled)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                llm_manager.call_model(
                    [{"role": "user", "content": "hi"}],
                    provider="openai",
                    model="gpt-4o",
                )
            )
        # Breaker must NOT be wedged in HALF_OPEN with _probe_in_flight=True
        assert cb._probe_in_flight is False
        assert cb.allow_request() is True
