"""Tests for AnthropicAdapter native Messages API request/response mapping.

Covers:
  1. Endpoint URL resolves to /v1/messages.
  2. System messages extracted to top-level system parameter (not in messages).
  3. CP-12 prompt-caching system blocks preserved in top-level system parameter.
  4. max_tokens default (4096) and custom override.
  5. User and assistant message blocks transformation.
  6. Assistant tool_calls mapped to tool_use blocks.
  7. Tool result messages (role "tool") mapped to user tool_result blocks.
  8. Consecutive same-role turns merged to guarantee alternating roles.
  9. First message role guaranteed to be 'user'.
  10. OpenAI-style tools mapped to Anthropic input_schema tools.
  11. Provider flags do not inject legacy 'functions' key.
  12. Response normalization: text, tool_use, stop_reason, usage tokens.
  13. extract_tool_calls on raw and normalized responses.
  14. generate() end-to-end with mocked response.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock
import pytest

from src.core.inference.adapters.anthropic_adapter import AnthropicAdapter
from src.core.context.context_builder import SYSTEM_PROMPT_DYNAMIC_BOUNDARY


def test_endpoint_and_default_max_tokens():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    messages = [{"role": "user", "content": "Hello"}]
    payload, ep = adapter._build_payload(messages, "claude-3-5-sonnet-20241022", {})
    assert ep == "https://api.anthropic.com/v1/messages"
    assert payload["max_tokens"] == 4096
    assert payload["model"] == "claude-3-5-sonnet-20241022"


def test_custom_max_tokens_override():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    messages = [{"role": "user", "content": "Hello"}]
    payload, _ = adapter._build_payload(messages, "claude-3-5-sonnet-20241022", {"max_tokens": 1024})
    assert payload["max_tokens"] == 1024

    payload2, _ = adapter._build_payload(messages, "claude-3-5-sonnet-20241022", {"max_completion_tokens": "2048"})
    assert payload2["max_tokens"] == 2048


def test_system_message_extracted_to_top_level():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    messages = [
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Write a test."},
    ]
    payload, _ = adapter._build_payload(messages, "claude-3-5-sonnet-20241022", {})

    assert "system" in payload
    assert payload["system"] == [{"type": "text", "text": "You are a helpful coding assistant."}]
    # System message must NOT remain in the messages list
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "user"


def test_cp12_system_cache_control_blocks_preserved():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    large_static = "Instruction block " * 50
    content = f"{large_static}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic turn data"
    messages = [
        {"role": "system", "content": content},
        {"role": "user", "content": "Hi"},
    ]
    preprocessed = adapter._preprocess_messages(messages)
    payload, _ = adapter._build_payload(preprocessed, "claude-3-5-sonnet-20241022", {})

    assert "system" in payload
    sys_blocks = payload["system"]
    assert len(sys_blocks) == 2
    assert sys_blocks[0]["type"] == "text"
    assert sys_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert sys_blocks[1]["type"] == "text"
    assert "cache_control" not in sys_blocks[1]


def test_assistant_tool_calls_mapped_to_tool_use_blocks():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    messages = [
        {"role": "user", "content": "Check files"},
        {
            "role": "assistant",
            "content": "Looking at workspace",
            "tool_calls": [
                {
                    "id": "toolu_123",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
                }
            ],
        },
    ]
    payload, _ = adapter._build_payload(messages, "claude-3-5-sonnet-20241022", {})
    msgs = payload["messages"]
    assert len(msgs) == 2
    assistant_msg = msgs[1]
    assert assistant_msg["role"] == "assistant"
    blocks = assistant_msg["content"]
    assert len(blocks) == 2
    assert blocks[0] == {"type": "text", "text": "Looking at workspace"}
    assert blocks[1] == {
        "type": "tool_use",
        "id": "toolu_123",
        "name": "read_file",
        "input": {"path": "README.md"},
    }


def test_tool_results_and_consecutive_user_turn_merging():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    messages = [
        {"role": "user", "content": "Execute step"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "tool_a", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "tool_b", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "output 1"},
        {"role": "tool", "tool_call_id": "call_2", "content": "output 2"},
    ]
    payload, _ = adapter._build_payload(messages, "claude-3-5-sonnet-20241022", {})
    msgs = payload["messages"]

    # Turns should alternate strictly: user -> assistant -> user (merged tool results)
    assert len(msgs) == 3
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]

    # In turn 2: assistant has only tool_use blocks (no empty text block)
    assert len(msgs[1]["content"]) == 2
    assert all(b["type"] == "tool_use" for b in msgs[1]["content"])

    # In turn 3: user has 2 tool_result blocks merged
    turn3_blocks = msgs[2]["content"]
    assert len(turn3_blocks) == 2
    assert turn3_blocks[0] == {"type": "tool_result", "tool_use_id": "call_1", "content": "output 1"}
    assert turn3_blocks[1] == {"type": "tool_result", "tool_use_id": "call_2", "content": "output 2"}


def test_first_message_guaranteed_user():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    messages = [{"role": "assistant", "content": "I am an assistant."}]
    payload, _ = adapter._build_payload(messages, "claude-3-5-sonnet-20241022", {})
    msgs = payload["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_openai_tools_mapped_to_anthropic_input_schema():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Edits a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        }
    ]
    payload, _ = adapter._build_payload(
        [{"role": "user", "content": "Edit"}],
        "claude-3-5-sonnet-20241022",
        {"tools": openai_tools},
    )

    assert "tools" in payload
    tool = payload["tools"][0]
    assert tool["name"] == "edit_file"
    assert tool["description"] == "Edits a file"
    assert tool["input_schema"] == {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }
    # functions key must not be present
    assert "functions" not in payload


def test_apply_provider_flags_is_noop():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    payload = {"tools": [{"name": "test"}]}
    adapter._apply_provider_flags(payload)
    assert "functions" not in payload


def test_response_normalization_text_and_tools():
    raw_anthropic = {
        "id": "msg_01TEST",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I will inspect the file."},
            {
                "type": "tool_use",
                "id": "toolu_abc123",
                "name": "read_file",
                "input": {"path": "src/main.py"},
            },
        ],
        "model": "claude-3-5-sonnet-20241022",
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 45,
            "cache_read_input_tokens": 50,
        },
    }

    normalized = AnthropicAdapter._anthropic_to_openai_response(
        raw_anthropic, "claude-3-5-sonnet-20241022"
    )

    assert normalized["id"] == "msg_01TEST"
    assert normalized["model"] == "claude-3-5-sonnet-20241022"
    assert len(normalized["choices"]) == 1

    choice = normalized["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "I will inspect the file."
    assert choice["message"]["role"] == "assistant"
    assert len(choice["message"]["tool_calls"]) == 1

    tc = choice["message"]["tool_calls"][0]
    assert tc["id"] == "toolu_abc123"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "read_file"
    assert json.loads(tc["function"]["arguments"]) == {"path": "src/main.py"}

    usage = normalized["usage"]
    assert usage["prompt_tokens"] == 120
    assert usage["completion_tokens"] == 45
    assert usage["total_tokens"] == 165
    assert usage["cache_read_input_tokens"] == 50


def test_extract_tool_calls_from_raw_and_normalized():
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    raw_anthropic = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "bash",
                "input": {"command": "pytest"},
            }
        ],
    }

    # Raw format
    calls_raw = adapter.extract_tool_calls(raw_anthropic)
    assert len(calls_raw) == 1
    assert calls_raw[0]["name"] == "bash"
    assert calls_raw[0]["args"] == {"command": "pytest"}

    # Normalized format
    normalized = AnthropicAdapter._anthropic_to_openai_response(
        raw_anthropic, "claude-3-5-sonnet-20241022"
    )
    calls_norm = adapter.extract_tool_calls(normalized)
    assert len(calls_norm) == 1
    assert calls_norm[0]["name"] == "bash"
    assert calls_norm[0]["args"] == {"command": "pytest"}


def test_generate_end_to_end_mocked(monkeypatch):
    adapter = AnthropicAdapter(api_key="sk-ant-test")

    raw_response = {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Anthropic response"}],
        "model": "claude-3-5-sonnet-20241022",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = raw_response
    mock_resp.raise_for_status = MagicMock()

    monkeypatch.setattr(adapter, "_safe_post", lambda *args, **kwargs: mock_resp)

    res = adapter.generate([{"role": "user", "content": "Ping"}])
    assert res["ok"] is True
    assert res["provider"] == "anthropic"
    assert len(res["choices"]) == 1
    assert res["choices"][0]["message"]["content"] == "Anthropic response"
    assert res["choices"][0]["finish_reason"] == "stop"
    assert res["prompt_tokens"] == 10
    assert res["completion_tokens"] == 5
    assert res["total_tokens"] == 15


def test_anthropic_adapter_custom_base_url():
    """AnthropicAdapter must honor caller-supplied base_url."""
    adapter = AnthropicAdapter(api_key="sk-ant-test", base_url="https://anthropic.custom-proxy.internal/v1/")
    assert adapter.base_url == "https://anthropic.custom-proxy.internal/v1"


@pytest.mark.asyncio
async def test_anthropic_adapter_streaming_assembly():
    """AnthropicAdapter stream chunks must be assembled by call_adapter_with_fallbacks."""
    import asyncio
    from src.core.inference.llm_manager import _consume_sse_stream
    from src.core.inference.runtime_call import call_adapter_with_fallbacks

    adapter = AnthropicAdapter(api_key="sk-ant-test")

    sse_lines = [
        b'event: message_start\n',
        b'data: {"type": "message_start", "message": {"id": "msg_stream_1"}}\n',
        b'event: content_block_start\n',
        b'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}\n',
        b'event: content_block_delta\n',
        b'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}\n',
        b'event: content_block_delta\n',
        b'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " World!"}}\n',
        b'event: message_delta\n',
        b'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}\n',
        b'event: message_stop\n',
        b'data: {"type": "message_stop"}\n',
    ]

    mock_resp = MagicMock()
    mock_resp.iter_lines.return_value = sse_lines
    mock_resp.status_code = 200

    adapter._execute_with_retry = MagicMock(return_value=mock_resp)

    async def _run_coro(loop, executor, fn):
        return fn()

    res = await call_adapter_with_fallbacks(
        adapter=adapter,
        messages=[{"role": "user", "content": "Hi"}],
        model="claude-3-5-sonnet-20241022",
        stream=True,
        format_json=False,
        call_extra_args={},
        run_with_correlation=_run_coro,
        consume_sse_stream=_consume_sse_stream,
    )
    assert res.get("ok") is True
    assert res.get("text") == "Hello World!"
    assert res.get("streamed") is True


@pytest.mark.asyncio
async def test_ollama_adapter_streaming_assembly():
    """OllamaAdapter streaming generator chunks must be assembled by call_adapter_with_fallbacks."""
    from src.core.inference.adapters.ollama_adapter import OllamaAdapter
    from src.core.inference.llm_manager import _consume_sse_stream
    from src.core.inference.runtime_call import call_adapter_with_fallbacks

    adapter = OllamaAdapter(name="ollama", base_url="http://localhost:11434")

    def _mock_gen():
        yield {"model": "llama3:8b", "message": {"role": "assistant", "content": "Part 1 "}, "done": False}
        yield {
            "model": "llama3:8b",
            "message": {
                "role": "assistant",
                "content": "Part 2",
                "tool_calls": [{"name": "test_tool", "arguments": {"x": 1}}],
            },
            "done": True,
            "prompt_eval_count": 10,
            "eval_count": 20,
        }

    mock_generate = {
        "ok": True,
        "provider": "ollama",
        "model": "llama3:8b",
        "choices": [],
        "raw": _mock_gen(),
    }
    adapter.generate = MagicMock(return_value=mock_generate)

    async def _run_coro(loop, executor, fn):
        return fn()

    res = await call_adapter_with_fallbacks(
        adapter=adapter,
        messages=[{"role": "user", "content": "Hi"}],
        model="llama3:8b",
        stream=True,
        format_json=False,
        call_extra_args={},
        run_with_correlation=_run_coro,
        consume_sse_stream=_consume_sse_stream,
    )
    assert res.get("ok") is True
    assert res.get("text") == "Part 1 Part 2"
    assert res.get("streamed") is True
    assert len(res.get("choices", [])) == 1
    choice = res["choices"][0]
    assert choice["message"]["content"] == "Part 1 Part 2"
    assert "tool_calls" in choice
    assert "tool_calls" in choice["message"]
    assert res["prompt_tokens"] == 10
    assert res["completion_tokens"] == 20
    assert res["total_tokens"] == 30

