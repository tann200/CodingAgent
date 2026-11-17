"""Regression tests for TASK-05: CircuitBreaker read-only state/is_open, streaming error checks, and auto_compactor pairing."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter
from src.core.inference.llm_manager import CircuitBreaker
from src.core.memory.auto_compactor import (
    AutoCompactConfig,
    _merge_compact_summaries,
    compact_messages,
    estimate_message_tokens,
)


class TestTask05CircuitBreakerReadOnly:
    def test_is_open_and_state_do_not_consume_probe_before_allow_request(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.005)
        cb.record_failure()
        assert cb.is_open() is True
        time.sleep(0.02)

        # Calling is_open() and reading .state multiple times stays read-only
        assert cb.is_open() is False
        assert cb.state == CircuitBreaker.HALF_OPEN
        assert cb.is_open() is False
        assert cb._probe_in_flight is False

        # Probe is claimed strictly by allow_request()
        assert cb.allow_request() is True
        assert cb._probe_in_flight is True
        assert cb.allow_request() is False


class TestTask05StreamingErrorsAndNullUsage:
    def _make_adapter(self) -> OpenAICompatibleAdapter:
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:1234/v1",
            models=["test-model"],
        )
        adapter.name = "test_compat"
        adapter.default_model = "test-model"
        return adapter

    def test_stream_http_401_and_500_return_ok_false_instead_of_masking(self):
        adapter = self._make_adapter()

        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.json.return_value = {"error": {"message": "Unauthorized"}}

        def _raise_401():
            raise RuntimeError("401 Unauthorized")

        resp_401.raise_for_status.side_effect = _raise_401

        with patch.object(adapter, "_safe_post", return_value=resp_401):
            out_401 = adapter.generate(
                [{"role": "user", "content": "hi"}], stream=True, timeout=7.5
            )
        assert out_401["ok"] is False
        assert out_401["raw"]["meta"]["status_code"] == 401

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        resp_429.json.return_value = {"error": {"message": "Rate limited"}}

        def _raise_429():
            raise RuntimeError("429 Too Many Requests")

        resp_429.raise_for_status.side_effect = _raise_429

        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.headers = {}
        resp_500.json.return_value = {"error": {"message": "Server error"}}

        def _raise_500():
            raise RuntimeError("500 Internal Server Error")

        resp_500.raise_for_status.side_effect = _raise_500

        with (
            patch.object(adapter, "_safe_post", side_effect=[resp_429, resp_500, resp_500]),
            patch("src.core.inference.adapters.openai_compat_adapter.time.sleep"),
        ):
            out_500 = adapter.generate([{"role": "user", "content": "hi"}], stream=True)
        assert out_500["ok"] is False
        assert out_500["raw"]["meta"]["status_code"] == 500

    def test_generate_handles_null_usage_and_forwards_timeout_and_format_json(self):
        adapter = self._make_adapter()
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.raise_for_status.return_value = None
        ok_resp.json.return_value = {
            "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
            "usage": None,
        }

        with patch.object(adapter, "_safe_post", return_value=ok_resp) as mock_post:
            out = adapter.generate(
                [{"role": "user", "content": "json"}],
                timeout=12.0,
                format_json=True,
            )
        assert out["ok"] is True
        assert out["cache_creation_input_tokens"] == 0
        assert mock_post.call_args.kwargs["timeout"] == 12.0
        assert mock_post.call_args.args[2].get("response_format") == {"type": "json_object"}


class TestTask05AutoCompactorPairingAndBudget:
    def test_compact_messages_never_leaves_orphan_tool_message(self):
        msgs = [
            {"role": "user", "content": "a " * 200},
            {"role": "assistant", "content": "b " * 200},
            {"role": "user", "content": "run tools"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
                    {"id": "c2", "function": {"name": "read_file", "arguments": '{"path":"b.py"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "contents of a"},
            {"role": "tool", "tool_call_id": "c2", "content": "contents of b"},
            {"role": "user", "content": "final question"},
        ]
        # preserve_recent=3 would naively slice into the middle of tool_calls + tool responses
        cfg = AutoCompactConfig(preserve_recent=3, max_tokens=50)
        result = compact_messages(msgs, cfg)

        assert result.removed_message_count > 0
        tail = result.compacted_messages[1:]
        assert tail[0]["role"] != "tool"
        # Both assistant tool_calls and matching tool responses stay together
        assert tail[0]["role"] == "assistant"
        assert tail[1]["role"] == "tool"
        assert tail[2]["role"] == "tool"
        assert tail[3]["role"] == "user"

    def test_estimate_message_tokens_includes_tool_calls(self):
        plain = {"role": "assistant", "content": ""}
        with_tc = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "function": {"name": "write_file", "arguments": "x" * 800}}
            ],
        }
        assert estimate_message_tokens(with_tc) > estimate_message_tokens(plain) + 150

    def test_merge_compact_summaries_bounds_recursive_growth(self):
        summary = None
        for i in range(40):
            new_s = (
                "<summary>\nConversation summary:\n"
                + "\n".join(f"- Highlight {i}_{j}: " + ("detail " * 15) for j in range(5))
                + "\n- Key timeline:\n"
                + f"  - Step {i}: action taken\n</summary>"
            )
            summary = _merge_compact_summaries(summary, new_s)
        assert summary is not None
        assert len(summary) <= 6000
