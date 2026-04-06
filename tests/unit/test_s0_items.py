"""tests/unit/test_s0_items.py — Regression tests for S0-A, S0-B, S0-C, D-07.

Covers:
- tokenizer.count_tokens / count_messages_tokens / fits_in_budget (S0-A / D-06)
- errors.AgentError / ErrorCode (S0-B)
- mock_adapter.MockAdapter (S0-C)
- event_bus.run_with_correlation (D-07)
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# S0-A / D-06 — tokenizer
# ---------------------------------------------------------------------------

class TestCountTokens:
    def test_empty_string_returns_zero(self):
        from src.core.inference.tokenizer import count_tokens
        assert count_tokens("") == 0

    def test_short_string_returns_positive(self):
        from src.core.inference.tokenizer import count_tokens
        n = count_tokens("hello world")
        assert n > 0

    def test_longer_text_more_tokens(self):
        from src.core.inference.tokenizer import count_tokens
        short = count_tokens("hi")
        long = count_tokens("hello world, this is a longer string with more content")
        assert long > short

    def test_model_hint_accepted(self):
        from src.core.inference.tokenizer import count_tokens
        # Should not raise regardless of model hint
        n = count_tokens("some text", model_hint="qwen3:14b")
        assert n > 0

    def test_count_messages_tokens(self):
        from src.core.inference.tokenizer import count_messages_tokens
        messages = [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi there"},
        ]
        total = count_messages_tokens(messages)
        assert total > 0

    def test_count_messages_empty_list(self):
        from src.core.inference.tokenizer import count_messages_tokens
        assert count_messages_tokens([]) == 0

    def test_fits_in_budget_true(self):
        from src.core.inference.tokenizer import fits_in_budget
        messages = [{"role": "user", "content": "hi"}]
        assert fits_in_budget(messages, budget=10_000) is True

    def test_fits_in_budget_false(self):
        from src.core.inference.tokenizer import fits_in_budget
        long_content = "word " * 10_000  # ~10k words → well over 100 token budget
        messages = [{"role": "user", "content": long_content}]
        assert fits_in_budget(messages, budget=100) is False

    def test_count_messages_handles_tool_calls(self):
        from src.core.inference.tokenizer import count_messages_tokens
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": '{"path": "foo.py"}'}}
                ],
            }
        ]
        total = count_messages_tokens(messages)
        assert total > 0


# ---------------------------------------------------------------------------
# S0-B — errors.AgentError
# ---------------------------------------------------------------------------

class TestAgentError:
    def test_basic_construction(self):
        from src.core.errors import AgentError, ErrorCode
        err = AgentError(ErrorCode.RATE_LIMIT, "429 returned", retryable=True)
        assert err.code is ErrorCode.RATE_LIMIT
        assert "429" in err.message
        assert err.retryable is True

    def test_str_includes_code(self):
        from src.core.errors import AgentError, ErrorCode
        err = AgentError(ErrorCode.TIMEOUT, "timed out")
        s = str(err)
        assert "timeout" in s.lower()

    def test_str_non_retryable(self):
        from src.core.errors import AgentError, ErrorCode
        err = AgentError(ErrorCode.PERMISSION_DENIED, "denied", retryable=False)
        assert "non-retryable" in str(err)

    def test_to_dict(self):
        from src.core.errors import AgentError, ErrorCode
        err = AgentError(ErrorCode.DOOM_LOOP, "looping", context={"turn": 5})
        d = err.to_dict()
        assert d["error_code"] == "doom_loop"
        assert d["message"] == "looping"
        assert d["retryable"] is True
        assert d["turn"] == 5

    def test_from_dict_round_trip(self):
        from src.core.errors import AgentError, ErrorCode
        err = AgentError(ErrorCode.BUDGET_EXCEEDED, "budget exceeded", retryable=False)
        restored = AgentError.from_dict(err.to_dict())
        assert restored.code is ErrorCode.BUDGET_EXCEEDED
        assert restored.retryable is False

    def test_from_dict_unknown_code(self):
        from src.core.errors import AgentError, ErrorCode
        restored = AgentError.from_dict({"error_code": "nonexistent_code", "message": "oops"})
        assert restored.code is ErrorCode.UNKNOWN

    def test_is_exception(self):
        from src.core.errors import AgentError, ErrorCode
        err = AgentError(ErrorCode.UNKNOWN, "something")
        assert isinstance(err, Exception)

    def test_all_error_codes_round_trip(self):
        from src.core.errors import AgentError, ErrorCode
        for code in ErrorCode:
            err = AgentError(code, "msg")
            d = err.to_dict()
            restored = AgentError.from_dict(d)
            assert restored.code is code


# ---------------------------------------------------------------------------
# S0-C — MockAdapter
# ---------------------------------------------------------------------------

class TestMockAdapter:
    def test_basic_response(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter(responses=["Hello world"])
        result = adapter.generate([{"role": "user", "content": "hi"}])
        assert result["ok"] is True
        assert result["content"] == "Hello world"

    def test_call_count_increments(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter(responses=["a", "b"])
        adapter.generate([{"role": "user", "content": "1"}])
        adapter.generate([{"role": "user", "content": "2"}])
        assert adapter.call_count == 2

    def test_repeats_last_when_exhausted(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter(responses=["only"])
        adapter.generate([])
        result = adapter.generate([])
        assert result["content"] == "only"

    def test_strict_raises_on_exhaustion(self):
        import pytest
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter(responses=["one"], strict=True)
        adapter.generate([])
        with pytest.raises(StopIteration):
            adapter.generate([])

    def test_callable_response(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter(responses=[lambda msgs: f"you said: {msgs[-1]['content']}"])
        result = adapter.generate([{"role": "user", "content": "hello"}])
        assert "hello" in result["content"]

    def test_call_log_records_messages(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter(responses=["ok"])
        msgs = [{"role": "user", "content": "test"}]
        adapter.generate(msgs)
        assert adapter.call_log[0] == msgs

    def test_reset(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter(responses=["x"])
        adapter.generate([])
        adapter.reset()
        assert adapter.call_count == 0
        assert adapter.call_log == []

    def test_get_models_returns_ok(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter()
        result = adapter.get_models_from_api()
        assert result["ok"] is True

    def test_stream_mode(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        adapter = MockAdapter(responses=["streamed"])
        result = adapter.generate([], stream=True)
        assert result["ok"] is True
        assert "raw" in result

    def test_requires_no_api_key(self):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        assert MockAdapter.REQUIRES_API_KEY is False


# ---------------------------------------------------------------------------
# D-07 — run_with_correlation
# ---------------------------------------------------------------------------

class TestRunWithCorrelation:
    def test_propagates_correlation_id(self):
        """Executor thread should see the same correlation ID as the caller."""
        from src.core.orchestration.event_bus import (
            run_with_correlation,
            set_correlation_id,
            get_correlation_id,
        )
        import asyncio

        set_correlation_id("test-cid-123")

        captured = []

        def _worker():
            captured.append(get_correlation_id())

        async def _run():
            loop = asyncio.get_running_loop()
            await run_with_correlation(loop, None, _worker)

        asyncio.run(_run())
        assert captured == ["test-cid-123"]

    def test_returns_coroutine(self):
        from src.core.orchestration.event_bus import run_with_correlation
        import asyncio

        async def _run():
            loop = asyncio.get_running_loop()
            result = await run_with_correlation(loop, None, lambda: 42)
            return result

        assert asyncio.run(_run()) == 42
